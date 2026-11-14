"""分阶段流水线。每个阶段读写 runs/<项目>/ 下的文件，可中断、可单镜重跑。

目录结构：
  runs/<id>/state.json   项目与镜头状态（按镜头 id）、记账簿
  runs/<id>/script.json  分镜脚本（可编辑；镜头有稳定 id，调整顺序不影响已生成素材）
  runs/<id>/refs/        用户参考图
  runs/<id>/images/      场景图候选  <镜头id>_c1.png …
  runs/<id>/videos/      镜头视频    <镜头id>.mp4
  runs/<id>/final.mp4    成片
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import cost as costmod
from .compose import compose, probe_duration
from .config import Settings
from .http import download as http_download
from .models import Seedance, Seedream, VideoTaskFailed, VolcTTS
from .script import compose_image_prompt, generate_script, script_cost_cny, validate_script

Log = Callable[[str], None]


class StageError(RuntimeError):
    pass


def _new_shot_state() -> dict:
    return {"status": "pending", "candidates": [], "selected": None, "video": None,
            "tasks": [], "error": None, "image_stale": False}


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.script_path = self.root / "script.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._lock = threading.RLock()

    @classmethod
    def create(cls, runs_dir: Path, description: str, duration: int, ratio: str,
               style: str = "", refs: list[str] | None = None, owner: str = "") -> "Project":
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        root = Path(runs_dir) / pid
        (root / "refs").mkdir(parents=True)
        saved = []
        for i, r in enumerate(refs or [], 1):
            src = Path(r)
            if not src.exists():
                raise StageError(f"参考图不存在：{r}")
            dst = root / "refs" / f"ref{i}{src.suffix.lower() or '.png'}"
            shutil.copy(src, dst)
            saved.append(str(dst.relative_to(root)))
        state = {"id": pid, "status": "draft", "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "owner": owner, "title": "",
                 "brief": {"description": description, "duration": duration, "ratio": ratio,
                           "style": style, "refs": saved},
                 "shots": {}, "ledger": [], "final": None}
        (root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return cls(root)

    # ---- 持久化 ----
    def save(self) -> None:
        with self._lock:
            tmp = self.state_path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    @property
    def has_script(self) -> bool:
        return self.script_path.exists()

    @property
    def script(self) -> dict:
        if not self.script_path.exists():
            raise StageError("还没有脚本，先生成脚本")
        return json.loads(self.script_path.read_text(encoding="utf-8"))

    def write_script(self, script: dict) -> None:
        self.script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state["title"] = script["title"]

    def shot_state(self, sid: str) -> dict:
        st = self.state["shots"].setdefault(sid, _new_shot_state())
        st.setdefault("image_stale", False)
        return st

    def path(self, rel: str | None) -> Path | None:
        return self.root / rel if rel else None

    @property
    def refs(self) -> list[str]:
        return [str(self.root / r) for r in self.state["brief"]["refs"]]


def _status_after_video_cleared(st: dict) -> str:
    if st["selected"]:
        return "image_selected"
    return "image_ready" if st["candidates"] else "pending"


class Pipeline:
    def __init__(self, settings: Settings, project: Project, log: Log = print,
                 seedream: Seedream | None = None, seedance: Seedance | None = None,
                 script_client=None, downloader=http_download, tts=None):
        self.s = settings
        self.tts = tts
        self.p = project
        self.log = log
        self.seedream = seedream or Seedream(settings)
        self.seedance = seedance or Seedance(settings)
        self.script_client = script_client
        self.download = downloader
        self.ledger = costmod.Ledger(settings, project.state["ledger"])

    def _set_status(self, status: str) -> None:
        self.p.state["status"] = status
        self.p.save()

    def _record(self, *args, **kw) -> None:
        for msg in self.ledger.record(*args, **kw):
            self.log(msg)

    def _targets(self, shots: list[int] | None) -> list[dict]:
        """shots 为镜头序号（从 1 开始）。"""
        all_shots = self.p.script["shots"]
        if not shots:
            return all_shots
        wanted = set(shots)
        missing = wanted - {s["index"] for s in all_shots}
        if missing:
            raise StageError(f"没有这些镜头：{sorted(missing)}")
        return [s for s in all_shots if s["index"] in wanted]

    def estimate(self) -> costmod.Estimate | None:
        if not self.p.has_script:
            return None
        shots = self.p.script["shots"]
        return costmod.estimate(self.s, len(shots), sum(s["duration"] for s in shots))

    def _selected_rel(self, sid: str) -> str | None:
        st = self.p.shot_state(sid)
        return st["candidates"][st["selected"] - 1] if st["selected"] else None

    def _expected_last(self, shots: list[dict], pos: int) -> str | None:
        """设置了“过渡到下一镜”时，尾帧 = 下一镜选中的场景图。"""
        if shots[pos].get("link_next") and pos + 1 < len(shots):
            return self._selected_rel(shots[pos + 1]["id"])
        return None

    def _check_video_inputs(self) -> list[int]:
        """首帧/尾帧已变化的镜头，作废其视频。返回被作废的镜头序号。"""
        shots = self.p.script["shots"]
        cleared = []
        for pos, shot in enumerate(shots):
            st = self.p.shot_state(shot["id"])
            inputs = st.get("video_inputs")
            if not st["video"] or not inputs:
                continue
            if inputs.get("first") != self._selected_rel(shot["id"]) or \
                    inputs.get("last") != self._expected_last(shots, pos):
                st["video"] = None
                st["status"] = _status_after_video_cleared(st)
                cleared.append(shot["index"])
        if cleared:
            self.p.state["final"] = None
        return cleared

    # ---- 阶段 1：脚本 ----
    def run_script(self) -> dict:
        self._set_status("scripting")
        self.log("DeepSeek 正在生成脚本…")
        try:
            script, usage = generate_script(self.s, self.p.state["brief"], client=self.script_client)
        except Exception:
            self._set_status("failed")
            raise
        self._record("script", self.s.deepseek_model, script_cost_cny(self.s, usage), usage=usage)
        fixed = {c["name"]: c for c in self.p.state["brief"].get("characters") or []}
        for c in script["characters"]:  # 模板里的角色外观和音色以模板为准，保证跨项目一致
            if c["name"] in fixed:
                c["appearance"] = fixed[c["name"]]["appearance"]
                if fixed[c["name"]].get("voice"):
                    c["voice"] = fixed[c["name"]]["voice"]
        self.p.write_script(script)
        self.p.state["shots"] = {s["id"]: _new_shot_state() for s in script["shots"]}
        self.p.state["final"] = None
        self._set_status("script_review")
        total = sum(s["duration"] for s in script["shots"])
        self.log(f"《{script['title']}》{len(script['shots'])} 个镜头，共 {total} 秒")
        est = self.estimate()
        self.log("整片预估：")
        for line in est.lines():
            self.log(line)
        if est.total_usd > self.s.budget_usd:
            self.log(f"⚠ 预估超过 ${self.s.budget_usd:.0f} 上限，请减少镜头/时长或候选图数量")
        return script

    def update_script(self, new: dict) -> dict:
        """保存人工编辑的脚本。不删除已生成素材：
        画面提示词变化 → 标记场景图过期；视频提示词或时长变化 → 作废该镜视频。"""
        old = {s["id"]: s for s in self.p.script["shots"]}
        script = validate_script(new, self.s.seedance_durations)
        keep = set()
        for shot in script["shots"]:
            sid = shot["id"]
            keep.add(sid)
            st = self.p.shot_state(sid)
            prev = old.get(sid)
            if prev is None:
                continue
            if prev["image_prompt"] != shot["image_prompt"] or prev["characters"] != shot["characters"]:
                if st["candidates"]:
                    st["image_stale"] = True
            if prev["video_prompt"] != shot["video_prompt"] or prev["duration"] != shot["duration"]:
                if st["video"]:
                    st["video"] = None
                    st["status"] = _status_after_video_cleared(st)
        for sid in list(self.p.state["shots"]):
            if sid not in keep:
                del self.p.state["shots"][sid]
        self.p.write_script(script)
        self.p.state["final"] = None
        self._check_video_inputs()
        self.p.save()
        return script

    # ---- 阶段 2：场景图 ----
    def run_images(self, shots: list[int] | None = None) -> None:
        script = self.p.script
        targets = self._targets(shots)
        if not shots:  # 默认只跑还没有候选图、或提示词已改的镜头
            todo = [s for s in targets
                    if not self.p.shot_state(s["id"])["candidates"] or self.p.shot_state(s["id"])["image_stale"]]
            targets = todo or targets
            est = self.estimate()
            if est.total_usd + self.ledger.spent_usd > self.s.budget_usd:
                raise costmod.BudgetExceeded(
                    f"整片预估 ${est.total_usd:.2f} + 已花 ${self.ledger.spent_usd:.2f}"
                    f" 超过上限 ${self.s.budget_usd:.0f}，不允许提交")
        self._set_status("imaging")
        (self.p.root / "images").mkdir(exist_ok=True)
        price = self.s.pricing.seedream_per_image
        for shot in targets:
            sid = shot["id"]
            st = self.p.shot_state(sid)
            st.update(status="imaging", candidates=[], selected=None, video=None, error=None, image_stale=False)
            self.p.save()
            prompt = compose_image_prompt(script, shot)
            stamp = uuid.uuid4().hex[:4]  # 重出图时换文件名，避免浏览器缓存旧图
            for c in range(1, self.s.candidates_per_shot + 1):
                self.ledger.ensure_room(price, f"第 {shot['index']} 镜场景图")
                self.log(f"Seedream 第 {shot['index']} 镜 候选 {c}/{self.s.candidates_per_shot}")
                try:
                    url, usage = self.seedream.generate(prompt, self.p.state["brief"]["ratio"], self.p.refs)
                    rel = f"images/{sid}_{stamp}_c{c}.png"
                    self.download(url, self.p.root / rel)
                except Exception as e:
                    st.update(status="failed", error=str(e))
                    self.p.save()
                    self.log(f"  ✗ 失败：{e}")
                    break
                st["candidates"].append(rel)
                self._record("image", self.s.seedream_model, price, shot=shot["index"], usage=usage)
                self.p.save()
            if st["candidates"] and st["status"] != "failed":
                st["status"] = "image_ready"
            self.p.save()
        self._set_status("image_review")
        self.log("场景图已生成，请为每个镜头选图")

    def refine_image(self, shot_index: int, candidate: int, instruction: str) -> str:
        """以某张候选图为底图，按修改说明再出一张，追加为新候选（不影响已有候选和选择）。"""
        instruction = (instruction or "").strip()
        if not 2 <= len(instruction) <= 300:
            raise StageError("修改说明 2–300 字")
        script = self.p.script
        shot = next((s for s in script["shots"] if s["index"] == shot_index), None)
        if not shot:
            raise StageError(f"没有第 {shot_index} 镜")
        st = self.p.shot_state(shot["id"])
        if not 1 <= candidate <= len(st["candidates"]):
            raise StageError(f"第 {shot_index} 镜没有候选 {candidate}")
        if len(st["candidates"]) >= 12:
            raise StageError("这一镜候选图已有 12 张，请先重出这一镜")
        price = self.s.pricing.seedream_per_image
        self.ledger.ensure_room(price, f"第 {shot_index} 镜改图")
        base = self.p.root / st["candidates"][candidate - 1]
        prompt = (compose_image_prompt(script, shot)
                  + f"在第一张参考图的基础上修改：{instruction}。除修改处外，保持构图、人物和画风不变。")
        refs = [str(base)] + self.p.refs[:3]
        self.log(f"Seedream 第 {shot_index} 镜 基于候选 {candidate} 修改：{instruction}")
        prev_status = self.p.state["status"]
        self._set_status("imaging")
        try:
            url, usage = self.seedream.generate(prompt, self.p.state["brief"]["ratio"], refs)
            rel = f"images/{shot['id']}_{uuid.uuid4().hex[:4]}_r{len(st['candidates']) + 1}.png"
            self.download(url, self.p.root / rel)
        except Exception:
            self._set_status(prev_status if prev_status != "imaging" else "image_review")
            raise
        st["candidates"].append(rel)
        st.setdefault("refined", {})[rel] = {"from": candidate, "instruction": instruction}
        if st["status"] in ("pending", "failed"):
            st["status"] = "image_ready"
        self._record("image", self.s.seedream_model, price, shot=shot_index, usage=usage, note="refine")
        self._set_status(prev_status if prev_status not in ("imaging", "scripting") else "image_review")
        self.log(f"已生成候选 {len(st['candidates'])}，点选即可使用")
        return rel

    # ---- 阶段 3：选图 ----
    def select(self, choices: dict[int, int] | None = None, auto: bool = False) -> None:
        """choices: {镜头序号: 候选序号(从1开始)}"""
        for shot in self.p.script["shots"]:
            st = self.p.shot_state(shot["id"])
            pick = (choices or {}).get(shot["index"])
            if pick is None and auto and st["selected"] is None and st["candidates"]:
                pick = 1
            if pick is None:
                continue
            if not 1 <= pick <= len(st["candidates"]):
                raise StageError(f"第 {shot['index']} 镜只有 {len(st['candidates'])} 张候选")
            if st["selected"] != pick:
                st.update(selected=pick, video=None, status="image_selected")  # 换图作废旧视频
                self.p.state["final"] = None
        cleared = self._check_video_inputs()  # 上一镜若过渡到本镜，也要重做
        if cleared:
            self.log(f"以下镜头的过渡尾帧已变化，视频已作废：{cleared}")
        self.p.save()
        left = [s["index"] for s in self.p.script["shots"] if self.p.shot_state(s["id"])["selected"] is None]
        self.log("全部镜头已选图" if not left else f"还未选图的镜头：{left}")

    # ---- 阶段 4：镜头视频 ----
    def run_videos(self, shots: list[int] | None = None) -> None:
        targets = self._targets(shots)
        if not shots:  # 默认只跑还没有视频的镜头
            targets = [s for s in targets if not self.p.shot_state(s["id"])["video"]]
        if not targets:
            self.log("所有镜头都已有视频")
            return
        all_shots = self.p.script["shots"]
        pos_of = {s["id"]: i for i, s in enumerate(all_shots)}
        for s in targets:
            if self.p.shot_state(s["id"])["selected"] is None:
                raise StageError(f"第 {s['index']} 镜还没选场景图")
            pos = pos_of[s["id"]]
            if s.get("link_next") and pos + 1 < len(all_shots) and self._expected_last(all_shots, pos) is None:
                raise StageError(f"第 {s['index']} 镜设置了过渡到下一镜，请先给第 {s['index'] + 1} 镜选图")
        price = self.s.pricing.seedance_per_second
        self.ledger.ensure_room(sum(s["duration"] for s in targets) * price, f"{len(targets)} 个镜头视频")
        self._set_status("generating")
        self.p.state["final"] = None
        (self.p.root / "videos").mkdir(exist_ok=True)
        submit_lock = threading.Lock()
        in_flight = [0.0]  # 已提交未完成任务的预计花费，也计入预算

        def one(shot: dict) -> tuple[int, str | None]:
            idx, sid = shot["index"], shot["id"]
            st = self.p.shot_state(sid)
            cost_cny = shot["duration"] * price
            first_rel = self._selected_rel(sid)
            last_rel = self._expected_last(all_shots, pos_of[sid])
            attempts = 1 + max(0, self.s.video_auto_retry)
            err = None
            for attempt in range(1, attempts + 1):
                with submit_lock:  # 提交前检查预算，串行化避免并发超支
                    self.ledger.ensure_room(cost_cny + in_flight[0], f"第 {idx} 镜视频")
                    in_flight[0] += cost_cny
                    try:
                        task_id = self.seedance.submit(shot["video_prompt"], self.p.path(first_rel),
                                                       shot["duration"], self.p.path(last_rel))
                    except Exception as e:  # 提交被拒（参数/审核），重试无意义
                        in_flight[0] -= cost_cny
                        st.update(status="failed", error=str(e))
                        self.p.save()
                        return idx, str(e)
                    st["tasks"].append({"id": task_id, "at": time.strftime("%H:%M:%S"),
                                        "mode": "first_last" if last_rel else "first"})
                    st.update(status="video_running", error=None, video=None)
                    self.p.save()
                self.log(f"Seedance 第 {idx} 镜已提交 {task_id}" + ("（首尾帧过渡）" if last_rel else "")
                         + (f"，第 {attempt} 次尝试" if attempt > 1 else ""))
                try:
                    task = self.seedance.poll(task_id)
                    rel = f"videos/{sid}_{task_id[-6:]}.mp4"
                    self.download(task["content"]["video_url"], self.p.root / rel)
                except VideoTaskFailed as e:  # 生成失败（通常不计费），自动重试
                    with submit_lock:
                        in_flight[0] -= cost_cny
                    err = str(e)
                    if attempt < attempts:
                        self.log(f"  第 {idx} 镜生成失败，自动重试：{e}")
                    continue
                except Exception as e:  # noqa: BLE001  下载等错误
                    with submit_lock:
                        in_flight[0] -= cost_cny
                    err = str(e)
                    break
                with submit_lock:
                    in_flight[0] -= cost_cny
                    st.update(status="succeeded", video=rel,
                              video_inputs={"first": first_rel, "last": last_rel})
                    self._record("video", self.s.seedance_model, cost_cny,
                                 shot=idx, usage=task.get("usage"), note=task_id)
                    self.p.save()
                return idx, None
            with submit_lock:
                st.update(status="failed", error=err)
                self.p.save()
            return idx, err

        errors = []
        with ThreadPoolExecutor(max_workers=self.s.max_parallel_videos) as ex:
            futures = [ex.submit(one, s) for s in targets]
            for f in as_completed(futures):
                try:
                    idx, err = f.result()
                except costmod.BudgetExceeded as e:
                    errors.append(str(e))
                    continue
                self.log(f"  {'✓' if not err else '✗'} 第 {idx} 镜{'' if not err else '：' + err}")
                if err:
                    errors.append(f"第 {idx} 镜：{err}")
        self._set_status("shot_review")
        if errors:
            self.log("有镜头失败，可单独重跑：\n  " + "\n  ".join(errors))

    # ---- 阶段 5：合成 ----
    def _narration_audio(self, shots: list[dict], clip_lengths: list[float], voice: str | None) -> list[tuple[float, Path]]:
        """为每镜旁白合成语音（按文本+音色缓存），返回 [(起始秒, 音频文件)]。"""
        tts = self.tts or VolcTTS(self.s)
        voice = voice or self.s.tts_voice
        char_voice = {c["name"]: c["voice"] for c in self.p.script["characters"] if c.get("voice")}
        audio_dir = self.p.root / "audio"
        audio_dir.mkdir(exist_ok=True)
        tracks, t0 = [], 0.0
        for shot, length in zip(shots, clip_lengths):
            text = shot["narration"].strip()
            if text:
                v = char_voice.get(shot.get("speaker") or "", voice)  # 角色台词用角色音色，旁白用默认音色
                key = hashlib.sha1(f"{v}|{text}".encode("utf-8")).hexdigest()[:12]
                path = audio_dir / f"{key}.mp3"
                if not path.exists():
                    price = len(text) / 10000 * self.s.pricing.tts_per_10k_chars
                    self.ledger.ensure_room(price, f"第 {shot['index']} 镜配音")
                    self.log(f"TTS 第 {shot['index']} 镜：{text[:20]}")
                    path.write_bytes(tts.synthesize(text, v))
                    self._record("tts", v, price, shot=shot["index"], usage={"chars": len(text)})
                    self.p.save()
                spoken = probe_duration(path)
                if spoken > length + 0.3:
                    self.log(f"⚠ 第 {shot['index']} 镜旁白 {spoken:.1f} 秒，超过镜头 {length:.1f} 秒，会和下一镜重叠，建议精简")
                tracks.append((t0, path))
            t0 += length
        return tracks

    def run_compose(self, bgm: str | None = None, tts: bool = False, voice: str | None = None,
                    burn_subs: bool = False) -> Path:
        shots = self.p.script["shots"]
        missing = [s["index"] for s in shots if not self.p.shot_state(s["id"])["video"]]
        if missing:
            raise StageError(f"这些镜头还没有视频：{missing}")
        if tts and not (self.tts or self.s.tts_enabled):
            raise StageError("未配置 TTS_APPID / TTS_TOKEN，无法配音")
        self._set_status("rendering")
        clips = [self.p.path(self.p.shot_state(s["id"])["video"]) for s in shots]
        name = f"final_{uuid.uuid4().hex[:4]}.mp4"
        try:
            narr = []
            if tts and any(s["narration"].strip() for s in shots):
                narr = self._narration_audio(shots, [probe_duration(c) for c in clips], voice)
            out = compose(clips, [s["narration"] for s in shots], self.p.state["brief"]["ratio"],
                          self.p.root / name, Path(bgm) if bgm else None, narration=narr, burn_subs=burn_subs)
        except Exception:
            self._set_status("shot_review")
            raise
        shutil.rmtree(self.p.root / "_compose", ignore_errors=True)
        old = self.p.state.get("final")
        if old and (self.p.root / old).exists():
            (self.p.root / old).unlink()
            srt = (self.p.root / old).with_suffix(".srt")
            if srt.exists():
                srt.unlink()
        for f in (self.p.state.get("exports") or {}).values():  # 成片变了，旧的其他画幅版本作废
            (self.p.root / f).unlink(missing_ok=True)
        self.p.state["exports"] = {}
        self.p.state["final"] = name
        self.p.state["final_meta"] = {"tts": bool(narr), "bgm": bool(bgm), "voice": (voice or self.s.tts_voice) if narr else None,
                                      "burn_subs": burn_subs}
        self._set_status("done")
        self.log(f"成片已生成：{name}" + ("（含旁白配音）" if narr else ""))
        return out

    def export_ratio(self, ratio: str) -> Path:
        """把成片转成其他画幅：原画面完整保留，空白处用同一画面放大模糊填充。"""
        from .compose import OUT_SIZES, reframe
        final = self.p.state.get("final")
        if not final:
            raise StageError("还没有成片，先合成")
        if ratio not in OUT_SIZES:
            raise StageError(f"不支持的画幅 {ratio}")
        if ratio == self.p.state["brief"]["ratio"]:
            raise StageError("和原画幅相同，直接下载成片即可")
        prev = self.p.state["status"]
        self._set_status("rendering")
        name = f"export_{ratio.replace(':', 'x')}_{uuid.uuid4().hex[:4]}.mp4"
        try:
            out = reframe(self.p.root / final, self.p.root / name, ratio)
        finally:
            self._set_status(prev if prev != "rendering" else "done")
        exports = self.p.state.setdefault("exports", {})
        if exports.get(ratio):
            (self.p.root / exports[ratio]).unlink(missing_ok=True)
        exports[ratio] = name
        self.p.save()
        self.log(f"已导出 {ratio} 版本：{name}")
        return out

    def run_all(self, bgm: str | None = None) -> Path:
        """一键跑完（自动选第 1 张候选图），用于 P0 批量测试。"""
        self.run_script()
        self.run_images()
        self.select(auto=True)
        self.run_videos()
        return self.run_compose(bgm)

    def status_lines(self) -> list[str]:
        st = self.p.state
        lines = [f"项目 {st['id']}  状态 {st['status']}  画幅 {st['brief']['ratio']}"]
        if self.p.has_script:
            for s in self.p.script["shots"]:
                ss = self.p.shot_state(s["id"])
                sel = f"选图 {ss['selected']}" if ss["selected"] else "未选图"
                lines.append(f"  #{s['index']:<2} {s['duration']:>2}s  {ss['status']:<15} "
                             f"候选 {len(ss['candidates'])}  {sel}  {'有视频' if ss['video'] else ''}"
                             f"  {s['description'][:24]}")
        summ = self.ledger.summary()
        lines.append(f"已花费 ${summ['spent_usd']}（¥{summ['spent_cny']}）/ 上限 ${summ['budget_usd']:.0f}"
                     f"  明细 {summ['by_kind_cny']}")
        if st.get("final"):
            lines.append(f"成片：{self.p.root / st['final']}")
        return lines
