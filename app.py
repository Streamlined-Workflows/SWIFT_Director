#!/usr/bin/env python3
"""SWIFT Director server. Standard library only.

Serves the browser page, keeps each project in data/projects/<id>.json, forwards uploads and
media to ComfyUI, turns segments into ComfyUI jobs, and reports system load.

  COMFY_URL  ComfyUI address (default http://127.0.0.1:8188)
  PORT       port to listen on (default 8190)
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import assist
import config
import generator
import reference
import lore
import usage
import models
from identity_studio import api as identity_api
from audio import api as audio_api
from studio3d import api as studio3d_api
import workflows

ROOT = os.path.dirname(os.path.abspath(__file__))
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
PORT = int(os.environ.get("PORT", "8190"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
TRASH_DIR = os.path.join(DATA_DIR, "trash")
LEGACY_FILE = os.path.join(DATA_DIR, "project.json")
LOCK = threading.RLock()

EDITABLE = ("name", "start", "end", "type", "begin_image", "end_image", "prompt", "runs", "chosen")
PENDING = ("queued", "running")
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp")
COMFY_PORT = urllib.parse.urlsplit(COMFY).port or 8188
ASSIST = os.environ.get("ASSIST_URL", "http://127.0.0.1:8191").rstrip("/")


# ---------- projects on disk ----------

def new_id():
    return uuid.uuid4().hex[:8]


def empty_project(name="Untitled project"):
    now = time.time()
    return {"schema": config.SCHEMA, "id": new_id(), "name": name, "created": now, "updated": now, "song": None,
            "settings": {"width": 832, "height": 480}, "audio": [], "actors": [], "photos": [], "segments": [],
            "config": config.new_project_config()}


def project_path(pid):
    if not pid or not all(c.isalnum() for c in pid):
        raise ValueError("Unknown project.")
    return os.path.join(PROJECTS_DIR, pid + ".json")


def normalize(project):
    """Fill in fields that older project files do not have."""
    project.setdefault("name", "Untitled project")
    project.setdefault("created", time.time())
    project.setdefault("updated", project["created"])
    project.setdefault("settings", {"width": 832, "height": 480})
    project.setdefault("segments", [])
    project.setdefault("actors", [])
    project.setdefault("audio", [])
    project.setdefault("photos", [])
    project.setdefault("image_jobs", [])
    project.setdefault("lore", [])               # ids of the lore entries tagged to this storyboard
    project.setdefault("things", [])             # this storyboard's scene assets and props
    # Schema 2: the configuration block. A file from before it gets the built-in defaults, which reproduce the old
    # behaviour exactly. A file from a newer version keeps its own number and any fields this version does not know.
    project["config"] = config.clean(project.get("config"))
    project["generator"] = generator.clean(project.get("generator"), rating=project["config"]["identity"]["rating"])      # Asset Generator: prompts, references, choices
    project["schema"] = max(config.SCHEMA, project["schema"]) if isinstance(project.get("schema"), int) else config.SCHEMA
    project.setdefault("intake", {"kind": "song", "text": "", "brief": {}})
    project.setdefault("collection_id", None)
    if "generated" not in project:          # the Library's record of every picture made here; older projects are rebuilt from what survives
        made = re.compile(r"_[0-9a-f]{8}\.png$")
        found = [{"image": v["image"], "label": f"{a.get('name') or 'Actor'} · {v.get('label') or 'view'}", "kind": "actor_view", "actor_id": a.get("id")}
                 for a in project.get("actors", []) for v in a.get("views", []) if made.search(v.get("image") or "")]
        found += [{"image": ph["image"], "label": ph.get("name") or "Shot", "kind": "shot"}
                  for ph in project.get("photos", []) if made.search(ph.get("image") or "")]
        project["generated"] = [dict(g, id=new_id(), created=None) for g in found]
    if any("views" not in a for a in project["actors"]):
        migrate_actors(project)
    song = project.get("song")
    if song and song.get("filename") and not any(a.get("filename") == song["filename"] for a in project["audio"]):
        project["audio"].append({"id": new_id(), "name": song["filename"], "filename": song["filename"],
                                 "duration": song.get("duration", 0)})
    return project


def migrate_actors(project):
    """Older projects stored one entry per still. An actor is now one character with several views.

    Stills whose names share a first word (pilot_medium, pilot_fullbody) become views of one actor.
    """
    kept, groups = [], {}
    for entry in project["actors"]:
        if "views" in entry:
            kept.append(entry)
        elif entry.get("kind") == "photo":
            project["photos"].append({"id": entry.get("id") or new_id(), "name": entry.get("name", ""), "image": entry["image"]})
        else:
            words = re.split(r"[_\-\s]+", (entry.get("name") or "").strip(), maxsplit=1)
            groups.setdefault(words[0].lower(), []).append((entry, words))
    for items in groups.values():
        shared = len(items) > 1
        first_entry, first_words = items[0]
        actor = {"id": new_id(), "description": "", "views": [],
                 "name": first_words[0].capitalize() if shared and first_words[0] else (first_entry.get("name") or "Actor")}
        for entry, words in items:
            label = re.sub(r"[_\-]+", " ", words[1]).strip() if shared and len(words) > 1 else ""
            actor["views"].append({"id": entry.get("id") or new_id(), "image": entry["image"], "label": label})
        kept.append(actor)
    project["actors"] = kept


def load_project(pid):
    with LOCK:
        try:
            with open(project_path(pid)) as f:
                return normalize(json.load(f))
        except OSError:
            raise ValueError("That project no longer exists.")


def save_project(project, touch=True):
    with LOCK:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        if touch:
            project["updated"] = time.time()
        path = project_path(project["id"])
        with open(path + ".tmp", "w") as f:
            json.dump(project, f, indent=1)
        os.replace(path + ".tmp", path)
        return project


def migrate_legacy():
    """Move the single-project file from version 1 into the projects folder."""
    with LOCK:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        if os.path.exists(LEGACY_FILE) and not os.listdir(PROJECTS_DIR):
            try:
                with open(LEGACY_FILE) as f:
                    old = json.load(f)
                project = {**empty_project("My first project"), **old}
                save_project(normalize(project))
                os.replace(LEGACY_FILE, LEGACY_FILE + ".migrated")
            except (OSError, ValueError):
                pass


def list_projects():
    with LOCK:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        out = []
        for name in os.listdir(PROJECTS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                p = load_project(name[:-5])
            except ValueError:
                continue
            clips = [r for s in p["segments"] for r in s.get("results", []) if r.get("status") == "done"]
            thumb = next((s.get("begin_image") for s in p["segments"] if s.get("begin_image")), None)
            out.append({"id": p["id"], "name": p["name"], "created": p["created"], "updated": p["updated"],
                        "segments": len(p["segments"]), "clips": len(clips), "assets": sum(len(a.get("views", [])) for a in p["actors"]) + len(p["photos"]) + len(p["audio"]),
                        "song": (p.get("song") or {}).get("filename"), "thumb": thumb or next((v["image"] for a in p["actors"] for v in a.get("views", [])), None)})
        return sorted(out, key=lambda p: -p["updated"])


def everything():
    """Assets and finished clips of every project, for the All view in Assets and Library."""
    out = []
    for info in list_projects():
        try:
            p = load_project(info["id"])
        except ValueError:
            continue
        clips = [{"segment": s.get("name") or "Segment", "segment_id": s["id"], "chosen": s.get("chosen") == r["id"],
                  "id": r["id"], "files": r.get("files", []), "engine": r.get("engine", ""),
                  "finished": r.get("finished") or r.get("created")}
                 for s in p["segments"] for r in s.get("results", []) if r.get("status") == "done" and r.get("files")]
        out.append({"id": p["id"], "name": p["name"], "actors": p["actors"], "photos": p["photos"], "audio": p["audio"], "clips": clips,
                    "images": p.get("generated", [])})
    return out


def merge_project(pid, incoming):
    """Apply the browser's edits. Render results stay owned by the server."""
    with LOCK:
        current = load_project(pid)
        old = {s["id"]: s for s in current.get("segments", [])}
        segments = []
        for seg in incoming.get("segments", []):
            sid = seg.get("id") or new_id()
            kept = old.get(sid, {})
            out = {"id": sid, "results": kept.get("results", [])}
            for key in EDITABLE:
                out[key] = seg.get(key, kept.get(key))
            out["runs"] = max(1, min(8, int(out.get("runs") or 1)))
            out["type"] = "singing" if out.get("type") == "singing" else "story"
            if out["type"] == "singing":
                out["end_image"] = None                 # the lip sync engine only takes a start image
            segments.append(out)
        current["segments"] = segments
        for key in ("song", "actors", "audio", "photos", "things"):
            if key in incoming:
                current[key] = incoming[key]
        if incoming.get("name"):
            current["name"] = str(incoming["name"])[:80]
        current["settings"] = {**current.get("settings", {}), **incoming.get("settings", {})}
        if "config" in incoming:                 # only valid choices are taken; anything else keeps its stored value
            current["config"] = config.clean(incoming["config"], current.get("config"))
        return save_project(normalize(current))


# ---------- ComfyUI ----------

def comfy_json(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(COMFY + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return {"_http_error": e.code, **json.loads(body)}
        except ValueError:
            return {"_http_error": e.code, "error": body[:500]}


def describe_error(entry):
    for kind, info in (entry.get("status", {}).get("messages") or []):
        if kind == "execution_error":
            return f'{info.get("node_type", "node")}: {info.get("exception_message", "failed")}'.strip()[:400]
    return "ComfyUI reported an error."


def collect_files(entry):
    files = []
    for node_out in (entry.get("outputs") or {}).values():
        for value in node_out.values():
            if isinstance(value, list):
                files += [v for v in value if isinstance(v, dict) and v.get("filename")]
    return [{"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")}
            for f in files]


def comfy_bytes(path, timeout=60):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return r.read()


def import_output(file, name):
    """Copy a generated image from ComfyUI's output folder into its input folder so shots can use it."""
    data = comfy_bytes("/view?" + urllib.parse.urlencode({"filename": file["filename"], "subfolder": file.get("subfolder", ""),
                                                         "type": file.get("type", "output")}))
    boundary = "----storyboard" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(COMFY + "/upload/image", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["name"]


def refresh_statuses(pid):
    """Ask ComfyUI about every unfinished clip and still in a project and record what it says."""
    with LOCK:
        project = load_project(pid)
        released = release_waiting(project)
        clips = [r for s in project["segments"] for r in s.get("results", []) if r.get("status") in PENDING]
        stills = [j for j in project["image_jobs"] if j.get("status") in PENDING]
        if not clips and not stills:
            return save_project(project, touch=False) if released else project
        try:
            queue = comfy_json("/queue", timeout=10)
        except OSError:
            return project
        running = {item[1] for item in queue.get("queue_running", [])}
        waiting = {item[1] for item in queue.get("queue_pending", [])}
        for res in clips + stills:
            pid_ = res["prompt_id"]
            if pid_ in running:
                res["status"] = "running"
                continue
            if pid_ in waiting:
                res["status"] = "queued"
                continue
            try:
                entry = comfy_json("/history/" + pid_, timeout=10).get(pid_)
            except OSError:
                continue
            if not entry:
                if time.time() - res.get("created", 0) > 60:
                    res.update(status="error", error="ComfyUI no longer knows this job (it may have restarted).")
                continue
            if entry.get("status", {}).get("status_str") == "error":
                res.update(status="error", error=describe_error(entry))
                continue
            files = collect_files(entry)
            if res in stills:
                picture = next((f for f in files if f["filename"].lower().endswith((".png", ".jpg", ".jpeg", ".webp"))), None)
                if not picture:
                    res.update(status="error", error="ComfyUI finished but returned no image.")
                    continue
                try:
                    res.update(status="done", image=import_output(picture, res.get("save_as") or picture["filename"]),
                               finished=time.time())
                    actor = next((a for a in project.get("actors", []) if a.get("id") == res.get("actor_id")), None)
                    project["generated"].append({
                        "id": res["id"], "image": res["image"], "kind": res.get("kind"), "created": res["finished"], "seed": res.get("seed"),
                        "actor_id": res.get("actor_id"), "segment_id": res.get("segment_id"), "source": res.get("source"), "fix": res.get("fix"),
                        "slot": res.get("slot") if res.get("kind") in ("gen", "ref") else None,
                        "label": f"{actor.get('name') or 'Actor'} · {res.get('label')}" if actor
                                 else f"{res['owner']} · {res.get('label')}" if res.get("owner") else res.get("label") or "Picture"})
                except (OSError, ValueError, KeyError) as e:
                    res.update(status="error", error=f"Could not save the generated image: {e}")
            else:
                pictures = [f for f in files if f["filename"].lower().endswith(IMAGE_TYPES)]
                res.update(status="done", files=[f for f in files if f not in pictures] or files, finished=time.time())
                if pictures:                # the clip's final frame, kept so the next segment can continue from it
                    try:
                        res["last_image"] = import_output(pictures[0], pictures[0]["filename"])
                    except (OSError, ValueError, KeyError):
                        res["last_frame"] = pictures[0]
        return save_project(project, touch=False)


# ---------- actors: describe from images, generate views ----------

def find_actor(project, actor_id):
    actor = next((a for a in project["actors"] if a.get("id") == actor_id), None)
    if actor is None:
        raise ValueError("That actor was not found. Save and try again.")
    return actor


def actor_describe(pid, actor_id):
    actor = find_actor(load_project(pid), actor_id)
    views = [v["image"] for v in actor.get("views", [])][:4]
    if not views:
        raise ValueError("Add at least one view of this actor first.")
    images = []
    for image in views:
        try:
            images.append(base64.b64encode(comfy_bytes("/view?" + urllib.parse.urlencode({"filename": image, "type": "input"}))).decode())
        except OSError as e:
            raise ValueError(f"Could not read the view {image} from the server: {e}")
    return {"description": assist.describe_actor(actor.get("name", ""), images)}


def actor_generate(pid, actor_id, refs=None, views=None, extra=""):
    """Queue stills of an actor. `views` names the framings (default: the three standard ones). `refs` names which of
    the actor's pictures to use as references (default: the first three); an empty list means description only. `extra` is a one-off addition to the description."""
    with LOCK:
        project = load_project(pid)
        actor = find_actor(project, actor_id)
        description = (actor.get("description") or "").strip()
        own = [v["image"] for v in actor.get("views", [])]
        refs = own[:3] if refs is None else [r for r in refs if r in own][:4]
        wanted = [v for v in (views or assist.STANDARD_VIEWS) if v in dict(assist.VIEW_FRAMINGS)]
        if not wanted:
            raise ValueError("Choose which view to generate.")
        extra = str(extra or "").strip()[:600]
        if not description and not refs and not extra:
            raise ValueError("Write a description for this actor first, or add a view of them.")
        threading.Thread(target=assist.unload, daemon=True).start()
        made = []
        for label, framing in assist.VIEW_FRAMINGS:
            if label not in wanted:
                continue
            stem = f"{workflows.safe_name(actor.get('name') or 'actor')}_{workflows.safe_name(label)}"
            made.append(queue_still(project, assist.view_prompt(description, framing, bool(refs), extra), refs, stem,
                                    kind="actor_view", actor_id=actor_id, label=label))
        return {"jobs": made}


def project_audio_files(pid):
    """The audio a storyboard uploaded before the audio library existed, fetched from ComfyUI's input folder so
    Audio Lab can take it in. -> [(name, bytes)]. A file that cannot be fetched is skipped."""
    out = []
    for entry in load_project(pid).get("audio", []):
        filename = entry.get("filename")
        if not filename:
            continue
        try:
            query = urllib.parse.urlencode({"filename": filename, "type": "input"})
            with urllib.request.urlopen(f"{COMFY}/view?{query}", timeout=120) as r:
                out.append((entry.get("name") or filename, r.read()))
        except OSError:
            continue
    return out


def studio3d_reference(pid, artifact_id):
    """A Reference Generation picture as it is now, for the 3D studio. -> {image, state, expires, what} or None"""
    try:
        a = next((a for a in load_project(pid).get("reference", {}).get("artifacts", []) if a["id"] == artifact_id), None)
    except ValueError:
        return None
    return {"image": a["image"], "state": a["state"], "expires": a.get("expires"), "what": a.get("component") or a.get("role") or ""} if a else None


def studio3d_picture(pid, image):
    try:
        query = urllib.parse.urlencode({"filename": image, "type": "input"})
        with urllib.request.urlopen(f"{COMFY}/view?{query}", timeout=120) as r:
            return r.read()
    except OSError:
        return None


def studio3d_project_name(pid):
    try:
        return load_project(pid).get("name") or pid
    except ValueError:
        return "A project that no longer exists"


def studio3d_handovers(pid):
    try:
        return reference_view(pid)["handover"]
    except ValueError:
        return []


def make_room_for_language_model():
    """Before the language model is asked to write, release what ComfyUI is holding, if ComfyUI is idle.
    The two share one pool of memory on this machine. A render in progress is left alone and the model loads beside it."""
    if "comfy" in usage.release(comfy_json, {"llm"})["released"]:
        time.sleep(2)                              # ComfyUI lets go on its next idle pass; give it that moment


def models_in_use():
    """What performs each model role today. Milestone 2 turns these into choices; until then there is one per role."""
    m = workflows.MODELS
    return {
        "analysis": {"name": assist.MODEL, "runtime": "Ollama", "ready": True},
        "prompting": {"name": assist.MODEL, "runtime": "Ollama", "ready": True},
        "image": {"name": "FLUX.2 [dev]", "file": m["flux2"], "runtime": "ComfyUI", "ready": True, "note": "Licensed for non-commercial use."},
        "image_lora": {"name": "FLUX.2 turbo (8 steps)", "file": m["flux2_turbo"], "runtime": "ComfyUI", "ready": True},
        "singing_video": {"name": "InfiniteTalk on Wan 2.1", "file": m["talk_patch"], "runtime": "ComfyUI", "ready": True},
        "story_video": {"name": "Wan 2.1 image-to-video with lightx2v", "file": m["unet_i2v"], "runtime": "ComfyUI", "ready": True},
        "begin_end_video": {"name": "Wan 2.2 first-and-last-frame", "file": m["unet_22_high"], "runtime": "ComfyUI", "ready": False,
                            "note": "Built but not yet confirmed on this server; its model files may still need downloading."},
        "stt": {"name": "faster-whisper " + os.environ.get("WHISPER_MODEL", "large-v3-turbo"), "runtime": "Song analysis service", "ready": True},
        "tts": {"name": "", "runtime": "", "ready": False, "note": "No text-to-speech model is installed."},
    }


def stage_items(project):
    """Everything that is not a person and can appear in a frame: this storyboard's scene assets and props, then the
    world assets and world props of the lore tagged to it. Ranked in that order when reference slots run short."""
    out = [dict(t, kind="prop" if t.get("kind") == "prop" else "scene", world="") for t in project.get("things", []) if (t.get("name") or "").strip()]
    out.sort(key=lambda t: t["kind"] != "scene")
    for lid in project.get("lore", []):
        try:
            entry = lore.load(lid)
        except ValueError:
            continue
        out += [dict(a, world=entry["title"], lore_id=lid) for a in entry["assets"] if (a.get("name") or "").strip()]
    seen, unique = set(), []
    for t in out:                                 # a name can only mean one thing to the language model
        if t["name"].lower() not in seen:
            seen.add(t["name"].lower()); unique.append(t)
    return unique


def thing_generate(pid, thing_id, lore_id=None, refs=None, views=None, extra=""):
    """Queue pictures of a scene asset, world asset or prop. The finished pictures are added to it by the page."""
    with LOCK:
        project = load_project(pid)
        pool = lore.load(lore_id)["assets"] if lore_id else project.get("things", [])
        thing = next((t for t in pool if t.get("id") == thing_id), None)
        if not thing:
            raise ValueError("That asset no longer exists. Reload the page.")
        kind = thing.get("kind") if thing.get("kind") in assist.THING_VIEWS else "scene"
        own = [v["image"] for v in thing.get("views", [])]
        refs = own[:3] if refs is None else [r for r in refs if r in own][:4]
        extra = str(extra or "").strip()[:600]
        if not (thing.get("description") or "").strip() and not refs and not extra:
            raise ValueError("Write a description first, or add a picture of it.")
        framings = [f for f in assist.THING_VIEWS[kind] if not views or f[0] in views]
        if not framings:
            raise ValueError("Choose which picture to generate.")
        threading.Thread(target=assist.unload, daemon=True).start()
        name = thing.get("name") or assist.THING_WORDS[kind]
        return {"jobs": [queue_still(project, assist.thing_prompt(thing, framing, bool(refs), extra), refs,
                                     f"{workflows.safe_name(name)}_{workflows.safe_name(label)}", kind="thing_view", actor_id=thing_id,
                                     lore_id=lore_id or None, label=label, owner=name) for label, framing in framings]}


def image_fix(pid, image, mask, text, width, height, label, refs=None):
    """Queue an inpaint: repaint the masked area of `image` as `text` describes. The original is kept."""
    text = str(text or "").strip()[:800]
    if not image or not mask:
        raise ValueError("Paint over the area to fix first.")
    if not text:
        raise ValueError("Describe what the painted area should look like.")
    with LOCK:
        project = load_project(pid)
        actor = next((a for a in project.get("actors", []) if any(v.get("image") == image for v in a.get("views", []))), None)
        view = next((v for v in actor["views"] if v.get("image") == image), {}) if actor else {}
        label = (view.get("label") or str(label or "Picture"))[:60].replace(" (fixed)", "") + " (fixed)"
        prompt = (f"{text.rstrip('.')}. Only this part of the picture changes. It blends seamlessly with the rest of the image: "
                  "the same character, art style, lighting and colours as the reference image, no visible seam, no text.")
        threading.Thread(target=assist.unload, daemon=True).start()
        known = {v["image"] for t in stage_items(project) for v in t.get("views", [])}      # only pictures of this storyboard's assets and props
        extra = [r for r in (refs or []) if r in known][:3]
        if extra:
            prompt += " The painted area shows the object from the extra reference image" + ("s" if len(extra) > 1 else "") + ", with its shape, colours and markings kept."
        graph = lambda seed, prefix: workflows.build_fix(image, mask, prompt, seed, int(width or 1248), int(height or 720), prefix, refs=extra)
        stem = f"{workflows.safe_name((actor or {}).get('name') or 'picture')}_{workflows.safe_name(label)}"
        return {"job": queue_still(project, prompt, [], stem, graph=graph, kind="fix", source=image, label=label,
                                   actor_id=actor["id"] if actor else None, fix=text)}


def queue_still(project, prompt, refs, stem, graph=None, **fields):
    """Queue one FLUX.2 image and record it on the project. Call with LOCK held. `graph(seed, prefix)` overrides the standard graph."""
    rid = new_id()
    seed = random.randint(1, 2**31 - 1)
    stem = f"{stem}_{rid}"
    prefix = f"storyboard/{project['id']}/stills/{stem}"
    graph = graph(seed, prefix) if graph else workflows.build_image(prompt, refs, seed, prefix=prefix)
    reply = comfy_json("/prompt", {"prompt": graph, "client_id": "storyboard"})
    if reply.get("_http_error") or reply.get("node_errors") or not reply.get("prompt_id"):
        detail = json.dumps(reply.get("node_errors") or reply.get("error") or reply)
        if "not in" in detail and ".safetensors" in detail:
            raise ValueError("The FLUX.2 image model files are not on the server yet. " + detail[:300])
        raise ValueError("ComfyUI rejected the image job: " + detail[:500])
    job = {"id": rid, "prompt_id": reply["prompt_id"], "status": "queued", "seed": seed, "created": time.time(),
           "save_as": stem + ".png", "steps": workflows.IMAGE_STEPS, "passes": 1, **fields}
    project["image_jobs"].append(job)
    save_project(project, touch=False)
    return job


# ---------- Asset Generator ----------

def generator_models():
    """What performs each of the Asset Generator's model roles today."""
    use = models_in_use()
    nothing = {"name": "", "runtime": "", "ready": False}
    return {"prompting": use["prompting"], "image": use["image"], "image_lora": use["image_lora"],
            "background_removal": dict(nothing, note="No background removal model is installed."),
            "upscale": dict(nothing, note="No upscale model is installed.")}


def generator_view(pid):
    return generator.view(refresh_statuses(pid), generator_models())


def generator_library(pid):
    """Every picture this project already has, for choosing a reference without uploading: newest generated pictures first,
    then the cast's views, assets and props (the tagged worlds' included), uploaded photos, and the storyboard's own shots."""
    project = refresh_statuses(pid)
    out, seen = [], set()

    def add(image, label, group):
        if isinstance(image, str) and image and image not in seen:
            seen.add(image)
            out.append({"image": image, "label": str(label or "Picture")[:120], "group": group})
    for g in sorted(project.get("generated", []), key=lambda g: -(g.get("created") or 0)):
        add(g.get("image"), g.get("label"), "Generated pictures")
    for a in project.get("actors", []):
        for v in a.get("views", []):
            add(v.get("image"), f"{a.get('name') or 'Actor'} · {v.get('label') or 'view'}", "Actors")
    for t in stage_items(project):
        for v in t.get("views", []):
            add(v.get("image"), f"{t['name']} · {v.get('label') or 'picture'}" + (f" ({t['world']})" if t.get("world") else ""), "Assets and props")
    for ph in project.get("photos", []):
        add(ph.get("image"), ph.get("name"), "Photos")
    for i, seg in enumerate(project.get("segments", []), 1):
        add(seg.get("begin_image"), f"{i}. {seg.get('name') or 'Segment'} · begin shot", "Storyboard shots")
        add(seg.get("end_image"), f"{i}. {seg.get('name') or 'Segment'} · end shot", "Storyboard shots")
    return {"pictures": out}


def generator_save(pid, state):
    with LOCK:
        project = load_project(pid)
        project["generator"] = generator.clean(state, project.get("generator"))
        save_project(project, touch=False)
        return generator.view(project, generator_models())


def comfy_upload(name, data):
    """Put a PNG in ComfyUI's input folder. -> the name ComfyUI stored it as."""
    boundary = "----storyboard" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(COMFY + "/upload/image", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["name"]


def placed_png(data_url, what):
    """A PNG the page sent as a data URL, checked to be a PNG of the size pictures are made at."""
    try:
        data = base64.b64decode(str(data_url).split(",", 1)[-1], validate=True)
    except (ValueError, TypeError):
        data = b""
    size = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")) if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24 else None
    if size != generator.SIZE or len(data) > 30 * 1024 * 1024:
        raise ValueError(f"The {what} for the inserted reference did not arrive properly. Reload the page and try again.")
    return data


def generator_run(pid, box, state=None, placed=None):
    """Queue a box's pictures: as many as its count says, each from a different seed.

    When one reference is set to "Insert as it is", the page sends `placed`: that reference on a blank picture, and a mask
    that is black over it and white everywhere else. The inpainting graph draws everything except the reference and pastes
    the original back through the mask, so the reference's own pixels are untouched."""
    with LOCK:
        project = load_project(pid)
        if state is not None:
            project["generator"] = generator.clean(state, project.get("generator"))
        todo = generator.plan(project, box)
        graph = None
        if todo["insert"]:
            if not isinstance(placed, dict) or placed.get("image") != todo["insert"]:
                raise ValueError("A reference is set to be inserted, but the page did not send its picture. Reload the page and try again.")
            stamp = new_id()
            canvas_png, mask_png = placed_png(placed.get("canvas"), "picture"), placed_png(placed.get("mask"), "mask")      # both checked before either is stored
            canvas = comfy_upload(f"gen_{pid}_{box}_{stamp}_canvas.png", canvas_png)
            mask = comfy_upload(f"gen_{pid}_{box}_{stamp}_around.png", mask_png)
            graph = lambda seed, prefix: workflows.build_fix(canvas, mask, todo["prompt"], seed, generator.SIZE[0], generator.SIZE[1], prefix,
                                                             refs=todo["refs"], refs_first=True)
        project["image_jobs"] = [j for j in project["image_jobs"] if not (j.get("kind") == "gen" and j.get("slot") == box and j.get("status") == "error")]
        threading.Thread(target=assist.unload, daemon=True).start()      # the language model gives its memory back to the image model
        label = generator.TITLES[box]
        for i in range(todo["count"]):
            queue_still(project, todo["prompt"], todo["refs"], "gen_" + box, graph=graph, kind="gen", slot=box, label=f"Asset Generator · {label}")
        save_project(project, touch=False)
        return generator.view(project, generator_models())


def generator_describe(pid, image, use):
    """Have the language model put a reference picture into words (its look, or its general idea), for the prompt.
    The picture must be one this project can already see: a reference in the generator or a picture in its library."""
    project = load_project(pid)
    known = {r["image"] for box in generator.BOXES for r in project["generator"][box]["refs"]} | {p["image"] for p in generator_library(pid)["pictures"]}
    if not isinstance(image, str) or image not in known:
        raise ValueError("That reference picture is not part of this project. Add it again.")
    try:
        data = comfy_bytes("/view?" + urllib.parse.urlencode({"filename": image, "type": "input"}))
    except OSError as e:
        raise ValueError(f"Could not read the reference picture from the server: {e}")
    return {"text": assist.describe_reference(base64.b64encode(data).decode(), use), "use": use, "image": image, "model": assist.MODEL}


def generator_combine(pid, text, references):
    """Clean and Combine: one tidy passage from the Reference Prompt box's text."""
    load_project(pid)                                    # the project must exist; the text itself comes from the page
    try:
        count = int(references)
    except (TypeError, ValueError):
        count = 1
    return {"text": assist.combine_references(text, count), "limit": assist.REFERENCE_LIMIT * max(1, min(4, count)), "model": assist.MODEL}


def generator_reset(pid, mode):
    with LOCK:
        project = refresh_statuses(pid)
        moved = generator.reset(project, mode, project["config"]["identity"]["rating"])
        save_project(project, touch=False)
        return dict(generator.view(project, generator_models()), moved=moved)


def generator_apply(pid, layer, image):
    with LOCK:
        project = load_project(pid)
        project["generator"] = generator.apply(project, layer, image)
        save_project(project, touch=False)
        return generator.view(project, generator_models())


def generator_remove(pid, gid):
    """Take one generated picture out of its box. The file stays on the server; anything already using it is unaffected."""
    with LOCK:
        project = load_project(pid)
        project["generated"] = [g for g in project.get("generated", []) if not (g.get("id") == gid and g.get("kind") == "gen")]
        save_project(project, touch=False)
        return generator.view(project, generator_models())


# ---------- Reference Generation (docs/Reference_Generation_Design.md) ----------

# ComfyUI offers no way to delete a file, so a denied or expired picture's file can only be removed when this app can
# reach ComfyUI's input folder itself. Set COMFY_INPUT_DIR to that folder to turn it on; without it the record,
# prompt and settings are deleted and the file stays where ComfyUI keeps it.
COMFY_INPUT_DIR = os.environ.get("COMFY_INPUT_DIR", "")


def reference_forget(project, images):
    """Delete the files of pictures whose records were just removed, unless something else in the project still names
    them. -> how many files were deleted."""
    if not images or not COMFY_INPUT_DIR:
        return 0
    rest, base, count = json.dumps(project), os.path.realpath(COMFY_INPUT_DIR), 0
    for image in images:
        path = os.path.realpath(os.path.join(base, image))
        if json.dumps(image)[1:-1] in rest or not path.startswith(base + os.sep) or not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            count += 1
        except OSError:
            pass
    return count


def reference_out(project, **more):
    return dict(reference.view(project), files_deletable=bool(COMFY_INPUT_DIR), **more)


def reference_view(pid):
    with LOCK:
        project = refresh_statuses(pid)
        before = json.dumps(project.get("reference"), sort_keys=True)
        _, gone = reference.sync(project)
        reference_forget(project, gone)
        if json.dumps(project["reference"], sort_keys=True) != before:
            save_project(project, touch=False)
        return reference_out(project, expired=len(gone))


def reference_save(pid, state):
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        project["reference"] = reference.clean(state, project["reference"], new_id)
        save_project(project, touch=False)
        return reference_out(project)


def reference_run(pid, rid, state=None):
    """Queue a Simple sheet run: as many complete sheets as the count says, each from a different seed."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        if state is not None:
            project["reference"] = reference.clean(state, project["reference"], new_id)
        todo = reference.plan(project, rid)
        name = reference.find(project["reference"], rid)["name"]
        project["image_jobs"] = [j for j in project["image_jobs"] if not (j.get("kind") == "ref" and j.get("slot") == rid and j.get("status") == "error")]
        threading.Thread(target=assist.unload, daemon=True).start()
        graph = lambda seed, prefix: workflows.build_image(todo["prompt"], todo["sources"], seed, width=todo["width"], height=todo["height"], prefix=prefix)
        for _ in range(todo["count"]):
            queue_still(project, todo["prompt"], todo["sources"], "ref_" + rid, graph=graph, kind="ref", slot=rid, label=f"Reference sheet · {name}",
                        ref_prompt=todo["prompt"], ref_sources=todo["sources"], width=todo["width"], height=todo["height"], provider=reference.PROVIDER["name"])
        save_project(project, touch=False)
        return reference_out(project, queued=todo["count"])


def reference_views(pid, rid, row_id, what, state=None, preview=False):
    """Advanced mode: queue pictures for a row's views. `what` is "row" (every enabled, unlocked view), "missing" (enabled
    views with no picture) or one direction. Locked and switched-off views are never touched."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        if state is not None:
            project["reference"] = reference.clean(state, project["reference"], new_id)
        todo = reference.plan_views(project, rid, row_id, what)
        ref = reference.find(project["reference"], rid)
        row = reference.find_row(ref, row_id)
        if preview:                                              # what would happen, for the page to show before anything is queued
            save_project(project, touch=False)
            return reference_out(project, plan=[{k: v[k] for k in ("direction", "count", "width", "height", "used", "unused", "unavailable")} for v in todo],
                                 skipped=reference.targets(project["reference"], row, what))
        project["image_jobs"] = [j for j in project["image_jobs"] if not (j.get("kind") == "ref" and j.get("row") == row_id and j.get("status") == "error")]
        threading.Thread(target=assist.unload, daemon=True).start()
        for view in todo:
            graph = lambda seed, prefix, v=view: workflows.build_image(v["prompt"], v["sources"], seed, width=v["width"], height=v["height"], prefix=prefix)
            for _ in range(view["count"]):
                queue_still(project, view["prompt"], view["sources"], f"ref_{rid}_{view['direction']}", graph=graph, kind="ref", slot=rid,
                            label=f"Reference view · {ref['name']} · {row['label'] or row['role'] or 'row'} · {view['direction'].capitalize()}",
                            ref_prompt=view["prompt"], ref_sources=view["sources"], width=view["width"], height=view["height"], provider=reference.PROVIDER["name"],
                            row=row_id, direction=view["direction"], component=row["role"], type=row["type"] or ref["type"], subtype=row["subtype"] if row["type"] else ref["subtype"])
        save_project(project, touch=False)
        return reference_out(project, queued=[v["direction"] for v in todo])


def reference_place(pid, rid, row_id, direction, image, state=None):
    """Advanced mode: put an uploaded or Library picture into a view slot, or into the row as its complete-row picture."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        if state is not None:
            project["reference"] = reference.clean(state, project["reference"], new_id)
        art = reference.place(project, rid, row_id, direction or None, image, new_id)
        save_project(project, touch=False)
        return reference_out(project, placed=art["id"])


def reference_assemble(pid, rid, png, used, state=None):
    """Store a sheet the page assembled on a canvas from the chosen pictures. Nothing is generated: the PNG arrives finished."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        if state is not None:
            project["reference"] = reference.clean(state, project["reference"], new_id)
        ref = reference.find(project["reference"], rid)
        try:
            data = base64.b64decode(str(png).split(",", 1)[-1], validate=True)
        except (ValueError, TypeError):
            data = b""
        if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 64 or len(data) > 80 * 1024 * 1024:
            raise ValueError("The assembled sheet did not arrive properly. Try again.")
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        aid = new_id()
        # Checked before the file is stored, so a refused request leaves nothing behind.
        reference.assembled({"reference": json.loads(json.dumps(project["reference"]))}, rid, "", width, height, used, lambda: aid)
        image = comfy_upload(f"ref_{pid}_{rid}_{aid}_sheet.png", data)
        art = reference.assembled(project, rid, image, width, height, used, lambda: aid)
        project["generated"].append({"id": aid, "image": image, "kind": "ref", "created": art["created"], "slot": rid, "label": f"Reference sheet · {ref['name']} · assembled"})
        save_project(project, touch=False)
        return reference_out(project, assembled=aid)


def reference_check(pid, rid, row_id):
    """Have the language model compare a row's views and store what it finds. The model is asked outside the lock, since
    it can take a while; the result is stored only if the row still exists."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        ref, row, views = reference.to_check(project, rid, row_id)
        what = f"{row['label'] or row['role'] or ref['name']} ({reference.TYPES[row['type'] or ref['type']]['label'].lower()})"
    pictures = []
    for d, a in views:
        try:
            pictures.append((d, base64.b64encode(comfy_bytes("/view?" + urllib.parse.urlencode({"filename": a["image"], "type": "input"}))).decode()))
        except OSError as e:
            raise ValueError(f"Could not read the {d} view from the server: {e}")
    found = assist.compare_references(pictures, what, reference.ASPECTS_CHECKED)
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        reference.checked(project, rid, row_id, found, assist.MODEL)
        save_project(project, touch=False)
        return reference_out(project, checked=row_id)


def reference_export(pid, rid, profile):
    """What an export contains. Reading this changes nothing: the page fetches the pictures and builds the download."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        return reference.export_plan(project, rid, profile)


def reference_send(pid, rid, remove=None):
    """Hand a reference's chosen pictures to 3D Generation, or take a hand-over back out with `remove`."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        if remove:
            project["reference"]["handover"] = [h for h in project["reference"]["handover"] if h["id"] != remove]
        else:
            reference.send_to_3d(project, rid, new_id)
        save_project(project, touch=False)
        return reference_out(project)


def reference_decide(pid, ids, action, confirmed=False, force=False):
    """Approve or deny pictures. Approving one that needs other pending pictures changes nothing and returns `needs`
    until the page sends it again with confirmed set."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        ids = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
        if action == "approve":
            result = reference.approve(project, ids, bool(confirmed))
            if "needs" in result:
                return reference_out(project, needs=result["needs"])
            save_project(project, touch=False)
            return reference_out(project, approved=result["approved"])
        if action != "deny":
            raise ValueError("Unknown action.")
        # A locked view keeps its picture. `force` is for removing a whole row or reference project, which says so itself.
        if not force and reference.locked_pictures(project["reference"]) & set(ids):
            raise ValueError("That picture is in a locked view. Unlock the view to deny it.")
        held = reference.protected(project["reference"], ids)
        busy = [a["id"] for a in project["reference"]["artifacts"] if a["id"] in ids and reference.in_use(project, a)]
        if busy:
            raise ValueError("A generation that is still running is using this picture. Deny it when that has finished.")
        gone = reference.delete(project, ids)
        files = reference_forget(project, gone)
        save_project(project, touch=False)
        return reference_out(project, denied=len(gone), files_deleted=files, held=held)


def reference_extract(pid, rid, source, cuts):
    """Sheet Extraction: store the pictures the page cut out of a sheet. The page does the cutting, at the sheet's own
    pixels with no resizing, and sends each piece as a PNG; this checks each one and records it as a pending picture."""
    with LOCK:
        project = load_project(pid)
        reference.sync(project)
        ref = reference.find(project["reference"], rid)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Choose the sheet to extract from first.")
        if not isinstance(cuts, list) or not cuts:
            raise ValueError("Draw at least one region on the sheet first.")
        ready = []
        for cut in cuts[:reference.MAX_REGIONS]:                 # every piece is checked before any is stored
            region = reference._region(cut if isinstance(cut, dict) else {})
            try:
                data = base64.b64decode(str((cut or {}).get("png", "")).split(",", 1)[-1], validate=True)
            except (ValueError, TypeError, AttributeError):
                data = b""
            dims = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")) if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24 else None
            if not region or dims != (region["w"], region["h"]) or len(data) > 40 * 1024 * 1024:
                raise ValueError("A cut-out did not arrive properly, so nothing was extracted. Reload the page and try again.")
            ready.append((region, data))
        now, made = time.time(), []
        for region, data in ready:
            aid = new_id()
            image = comfy_upload(f"ref_{pid}_{rid}_{aid}.png", data)
            what = region["direction"].capitalize() or region["component"] or "Detail"
            project["reference"]["artifacts"].append({
                "id": aid, "project": rid, "role": "view", "image": image, "state": "pending", "created": now, "expires": now + reference.RETENTION,
                "width": region["w"], "height": region["h"], "upscaled": False, "requires": [], "origin": "extracted", "source": source[:300],
                "region": {k: region[k] for k in ("x", "y", "w", "h")}, "direction": region["direction"], "section": region["section"], "component": region["component"]})
            project["generated"].append({"id": aid, "image": image, "kind": "ref", "created": now, "slot": rid, "label": f"Reference view · {ref['name']} · {what}"})
            made.append(aid)
        save_project(project, touch=False)
        return reference_out(project, extracted=made)


def reference_sweep():
    """Once an hour, delete pending reference pictures whose ten days have run out, in every project, so they go even if
    nobody opens the page."""
    while True:
        for listed in list_projects():
            try:
                with LOCK:
                    project = load_project(listed["id"])
                    if not (project.get("reference") or {}).get("artifacts"):
                        continue
                    _, gone = reference.sync(project)
                    if gone:
                        reference_forget(project, gone)
                        save_project(project, touch=False)
            except (ValueError, OSError, KeyError):
                pass
        time.sleep(3600)


def generated_remove(pid, gid):
    """Take a picture out of the Library list. The file itself, and any actor or shot using it, are left alone."""
    with LOCK:
        project = load_project(pid)
        project["generated"] = [g for g in project.get("generated", []) if g.get("id") != gid]
        save_project(project, touch=False)
        return {"generated": project["generated"]}


def image_jobs_clear(pid, ids):
    with LOCK:
        project = load_project(pid)
        project["image_jobs"] = [j for j in project["image_jobs"] if j["id"] not in set(ids or [])]
        return save_project(project, touch=False)


def resolve_begin(project, seg):
    """Where a segment starts: (image, None), or (None, why it has to wait).

    A segment with its own begin shot is a new angle and uses that picture. A segment without one continues from the
    segment before it: from that segment's end shot if it has one, otherwise from the last frame of the clip you
    picked there. Until a clip is picked, the segment waits.
    """
    if seg.get("begin_image"):
        return seg["begin_image"], None
    index = project["segments"].index(seg)
    if index == 0:
        return None, None
    prev = project["segments"][index - 1]
    name = prev.get("name") or f"segment {index}"
    if prev.get("type") != "singing" and prev.get("end_image"):
        return prev["end_image"], None
    picked = next((r for r in prev.get("results", []) if r.get("id") == prev.get("chosen") and r.get("status") == "done"), None)
    if not picked:
        return None, f'Waiting for you to pick a clip on "{name}". This segment continues from its last frame.'
    if not picked.get("last_image") and picked.get("last_frame"):
        picked["last_image"] = import_output(picked["last_frame"], picked["last_frame"]["filename"])
    if not picked.get("last_image"):
        raise ValueError(f'The clip picked on "{name}" was made before SWIFT Director kept last frames. Render "{name}" again '
                         "and pick a new clip, or give this segment its own begin shot.")
    return picked["last_image"], None


def queue_clip(project, seg, begin, rid):
    """Send one render of a segment to ComfyUI and return the fields that describe the queued job."""
    seed = random.randint(1, 2**31 - 1)
    settings = project.get("settings", {})
    prefix = f"storyboard/{project['id']}/{workflows.safe_name(seg.get('name'))}_{rid}"
    graph = workflows.build(dict(seg, begin_image=begin), (project.get("song") or {}).get("filename"), seed,
                            width=int(settings.get("width", 832)), height=int(settings.get("height", 480)), prefix=prefix)
    reply = comfy_json("/prompt", {"prompt": graph, "client_id": "storyboard"})
    if reply.get("_http_error") or reply.get("node_errors") or not reply.get("prompt_id"):
        detail = json.dumps(reply.get("node_errors") or reply.get("error") or reply)
        if "not in" in detail and ".safetensors" in detail:
            raise ValueError("A model file this shot needs is not on the server yet. " + detail[:400])
        raise ValueError("ComfyUI rejected the job: " + detail[:600])
    return {"prompt_id": reply["prompt_id"], "seed": seed, "status": "queued", "created": time.time(),
            "engine": workflows.engine(seg), "prompt": seg.get("prompt", ""), "begin": begin, **workflows.plan(seg)}


def release_waiting(project):
    """Start any held renders whose starting picture is now known. Returns True if anything changed. Call with LOCK held."""
    changed = False
    for seg in project["segments"]:
        held = [r for r in seg.get("results", []) if r.get("status") == "waiting"]
        if not held:
            continue
        try:
            begin, wait = resolve_begin(project, seg)
            if wait:
                if any(r.get("wait") != wait for r in held):
                    for r in held:
                        r["wait"] = wait
                    changed = True
                continue
            for r in held:
                r.update(queue_clip(project, seg, begin, r["id"]))
                r.pop("wait", None)
        except (ValueError, OSError) as e:
            for r in held:
                if r.get("status") == "waiting":
                    r.update(status="error", error=str(e))
        changed = True
    return changed


def render_all(pid):
    """Queue every segment's listed runs. Segments that cannot render are reported, not fatal."""
    queued, skipped = 0, []
    for seg in load_project(pid)["segments"]:
        try:
            queued += len(render_segment(pid, seg["id"], hold=False))
        except ValueError as e:
            skipped.append({"name": seg.get("name") or "Unnamed segment", "reason": str(e)})
    return {"queued": queued, "skipped": skipped}


def render_segment(pid, segment_id, runs=None, hold=True):
    """Queue a segment's runs. With hold=False (overnight batches) a segment that would have to wait is skipped instead."""
    with LOCK:
        project = load_project(pid)
        seg = next((s for s in project["segments"] if s["id"] == segment_id), None)
        if seg is None:
            raise ValueError("Segment not found. Save the storyboard and try again.")
        begin, wait = resolve_begin(project, seg)
        if not begin and not wait:
            raise ValueError("This segment has no begin shot yet.")
        if wait and not hold:
            index = project["segments"].index(seg)
            raise ValueError(f'continues from "{project["segments"][index - 1].get("name") or "the segment before it"}", which has no picked clip yet')
        threading.Thread(target=assist.unload, daemon=True).start()      # give the language model's memory back
        count = max(1, min(8, int(runs or seg.get("runs") or 1)))
        made = []
        for _ in range(count):
            rid = new_id()
            if wait:
                result = {"id": rid, "status": "waiting", "wait": wait, "files": [], "created": time.time(),
                          "engine": workflows.engine(seg), "prompt": seg.get("prompt", ""), **workflows.plan(seg)}
            else:
                result = {"id": rid, "files": [], **queue_clip(project, seg, begin, rid)}
            seg.setdefault("results", []).append(result)
            made.append(result)
            save_project(project)
        return made


def cancel_waiting(pid):
    with LOCK:
        project = load_project(pid)
        for seg in project["segments"]:
            seg["results"] = [r for r in seg.get("results", []) if r.get("status") != "waiting"]
        save_project(project, touch=False)


# ---------- Assisted Storyboard ----------

def assist_service(path, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(ASSIST + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except OSError as e:
        raise ValueError(f"The song analysis service is not running on the server ({e}). Run ~/SWIFT_Director/run.sh to start it.")


def song_key(project):
    song = (project.get("song") or {}).get("filename")
    if not song:
        raise ValueError("Choose a song for this project first.")
    return f"{project['id']}:{song}", song


def assist_analyze(pid, separate=False):
    """Start listening to the project's song. With separate, the voice is split from the music first and only the
    voice is transcribed."""
    with LOCK:
        project = load_project(pid)
        key, song = song_key(project)
        if project.get("assist"):
            project["assist"]["analysis_key"] = None          # a fresh run replaces the stored result
            save_project(project, touch=False)
    url = COMFY + "/view?" + urllib.parse.urlencode({"filename": song, "type": "input"})
    try:
        return assist_service("/analyze", {"key": key, "url": url, "separate": bool(separate)})
    except ValueError:
        if not separate:
            raise
        why = assist_service("/health", timeout=4).get("separation_reason")      # refused: say why if the service is there
        raise ValueError(why or "The song analysis service could not separate the voice.")


def assist_vocals(pid):
    """The separated voice of the project's song, as MP3 bytes, or None when it is not kept."""
    key, _ = song_key(load_project(pid))
    try:
        with urllib.request.urlopen(ASSIST + "/vocals?" + urllib.parse.urlencode({"key": key}), timeout=60) as r:
            return r.read()
    except OSError:
        return None


def assist_status(pid):
    """Poll the analysis job; when it finishes, store its result in the project."""
    project = load_project(pid)
    key, _ = song_key(project)
    stored = (project.get("assist") or {}).get("analysis_key") == key
    job = assist_service("/status?" + urllib.parse.urlencode({"key": key}))
    state = job.get("state", "none")
    if state == "done" and not stored:
        with LOCK:
            project = load_project(pid)
            result = dict(job["result"])
            lines = result.pop("lines", [])
            project["assist"] = {"analysis": result, "analysis_key": key, "lines": lines}
            save_project(project, touch=False)
    elif state == "none" and stored:
        state = "done"
    return {"state": state, "progress": job.get("progress", ""), "error": job.get("error")}


def assist_data(project, need_lines=False):
    data = project.get("assist") or {}
    if not data.get("analysis"):
        raise ValueError("Analyze the song first.")
    return data


def update_assist(pid, **changes):
    with LOCK:
        project = load_project(pid)
        project.setdefault("assist", {}).update(changes)
        return save_project(project, touch=False)


def assist_split(pid):
    data = assist_data(load_project(pid))
    cuts = assist.propose_cuts(data["analysis"], data.get("lines", []))
    return update_assist(pid, split=assist.name_and_merge(data["analysis"], data.get("lines", []), cuts))


def assist_apply_split(pid):
    with LOCK:
        project = load_project(pid)
        split = (project.get("assist") or {}).get("split")
        if not split:
            raise ValueError("Propose segments first.")
        project["segments"] = [{"id": new_id(), "name": s.get("name") or f"Segment {i + 1}", "start": s["start"], "end": s["end"],
                                "type": s.get("type", "story"), "begin_image": None, "end_image": None, "prompt": "",
                                "runs": 2, "chosen": None, "results": []} for i, s in enumerate(split)]
        project["assist"].pop("story_draft", None)
        project["assist"].pop("story", None)
        project["assist"].pop("shots", None)
        return save_project(project)


def assist_story(pid, direction, style, another):
    project = load_project(pid)
    data = assist_data(project)
    if not project["segments"]:
        raise ValueError("Put segments on the storyboard first (step 2).")
    old = data.get("story_draft") or {}
    previous = old.get("logline") if another else None
    ids = [s["id"] for s in project["segments"]]
    was = dict(zip(old.get("segment_ids", []), old.get("beats", [])))
    locks = [sid for sid in old.get("locks", []) if sid in ids and (was.get(sid) or "").strip()]     # locked beats survive a rewrite
    draft = assist.write_story(data["analysis"], data.get("lines", []), project["segments"], direction or "", style or "", previous,
                               lore=lore.for_model(project.get("lore"))[0], keep={ids.index(sid): was[sid] for sid in locks},
                               guidance=config.story_guidance(project.get("config")))
    draft["segment_ids"], draft["locks"] = ids, locks
    return update_assist(pid, story_draft=draft)


def assist_story_beat(pid, segment_id, text=None, locked=None):
    """Edit or lock one line of the story concept. A locked line cannot be edited and is kept when the story is rewritten."""
    with LOCK:
        project = load_project(pid)
        draft = (project.get("assist") or {}).get("story_draft")
        if not draft or segment_id not in draft.get("segment_ids", []):
            raise ValueError("That line of the story no longer exists. Reload the page.")
        i = draft["segment_ids"].index(segment_id)
        locks = [s for s in draft.get("locks", []) if s in draft["segment_ids"]]
        approved = project["assist"].get("story")
        same = bool(approved) and approved.get("logline") == draft.get("logline") and approved.get("segment_ids") == draft["segment_ids"]
        if text is not None:
            if segment_id in locks and locked is not False:
                raise ValueError("That line is locked. Unlock it to edit it.")
            new, before = str(text).strip()[:1200], draft["beats"][i]
            draft["beats"][i] = new
            if same:                                              # an approved story follows the edit, and so does a prompt that was copied from it
                approved["beats"][i] = new
                for seg in project["segments"]:
                    if seg["id"] == segment_id and (seg.get("prompt") or "").strip() == before.strip() and before.strip():
                        seg["prompt"] = new
        if locked is True and segment_id not in locks:
            locks.append(segment_id)
        if locked is False and segment_id in locks:
            locks.remove(segment_id)
        draft["locks"] = locks
        if same:
            approved["locks"] = list(locks)
        return save_project(project, touch=False)


def lore_listing():
    """Every lore entry, with the storyboards that use it."""
    projects = list_projects()
    using = {}
    for p in projects:
        try:
            for lid in load_project(p["id"]).get("lore", []):
                using.setdefault(lid, []).append(p["id"])
        except ValueError:
            continue
    return {"entries": [dict(e, projects=using.get(e["id"], [])) for e in lore.entries()],
            "storyboards": [{"id": p["id"], "name": p["name"]} for p in projects], "budget": lore.BUDGET}


def lore_tag(lid, pid, on):
    with LOCK:
        lore.load(lid)
        project = load_project(pid)
        tagged = [x for x in project.get("lore", []) if x != lid]
        if on:
            tagged.append(lid)
        project["lore"] = tagged
        save_project(project, touch=False)
    return lore_listing()


def assist_approve_story(pid, fill_prompts):
    with LOCK:
        project = load_project(pid)
        draft = (project.get("assist") or {}).get("story_draft")
        if not draft:
            raise ValueError("Write a story concept first.")
        project["assist"]["story"] = draft
        if fill_prompts:
            beats = dict(zip(draft.get("segment_ids", []), draft.get("beats", [])))
            for seg in project["segments"]:
                if not (seg.get("prompt") or "").strip() and beats.get(seg["id"]):
                    seg["prompt"] = beats[seg["id"]]
        return save_project(project)


def approved_story(project):
    story = (project.get("assist") or {}).get("story")
    if not story:
        raise ValueError("Approve a story concept first (step 3).")
    return story


def cast_members(project):
    """The chosen actors, with the role each plays: [{"name", "role", "description", "views"}]."""
    actors = {a["id"]: a for a in project.get("actors", [])}
    out = []
    for entry in (project.get("assist") or {}).get("cast", []):
        actor = actors.get(entry.get("actor_id"))
        if actor:
            out.append({"name": actor.get("name") or "Actor", "role": entry.get("role") or "",
                        "description": actor.get("description") or "", "views": actor.get("views", [])})
    return out


def assist_cast_save(pid, cast):
    with LOCK:
        project = load_project(pid)
        known = {a["id"] for a in project.get("actors", [])}
        clean = [{"actor_id": c["actor_id"], "role": str(c.get("role") or "")[:300]} for c in cast or []
                 if isinstance(c, dict) and c.get("actor_id") in known]
        return update_assist(pid, cast=clean)


def assist_recommend_cast(pid):
    project = load_project(pid)
    data = assist_data(project)
    ideas = assist.recommend_cast(approved_story(project), data["analysis"], data.get("lines", []), project["segments"],
                                  [a.get("name") for a in project.get("actors", []) if a.get("name")], lore=lore.for_model(project.get("lore"))[0])
    if not ideas:
        raise ValueError("The language model did not suggest anyone. Try again.")
    return update_assist(pid, cast_ideas=ideas)


def assist_shots(pid):
    project = load_project(pid)
    data = assist_data(project)
    if not project["segments"]:
        raise ValueError("Put segments on the storyboard first (step 2).")
    story = approved_story(project)
    shots = assist.write_shots(story, data["analysis"], data.get("lines", []), project["segments"], cast_members(project),
                               lore=lore.for_model(project.get("lore"))[0], things=stage_items(project),
                               guidance=config.shot_guidance(project.get("config")))
    for seg, end in zip(project["segments"], shots["ends"]):
        end["segment_id"] = seg["id"]
    old = (load_project(pid).get("assist") or {}).get("shots") or {}       # locked frames survive a rewrite, words and all
    if (old.get("first") or {}).get("locked"):
        shots["first"] = old["first"]
        if shots["ends"]:
            shots["ends"][0]["begin"] = old["first"]
    kept = {e.get("segment_id"): e for e in old.get("ends", [])}
    for end in shots["ends"]:
        was = kept.get(end["segment_id"]) or {}
        if was.get("locked"):
            end.update(text=was.get("text", ""), who=was.get("who", []), what=was.get("what", []), locked=True)
        if (was.get("begin") or {}).get("locked") and end is not shots["ends"][0]:
            end.update(start="new", begin=was["begin"])
    return update_assist(pid, shots=shots)


def assist_shots_save(pid, shots):
    with LOCK:
        project = load_project(pid)
        current = (project.get("assist") or {}).get("shots")
        if not current or not isinstance(shots, dict):
            raise ValueError("Write the shots first.")
        cast_names = [c["name"] for c in cast_members(project)]
        item_names = [t["name"] for t in stage_items(project)]

        def in_frame(frame, edit):
            """Which actors and assets a picture uses is the person's to set: the ticked names are kept, in the cast's and
            the assets' own order. Names that are no longer cast or no longer exist are dropped."""
            if isinstance(edit.get("who"), list):
                frame["who"] = [n for n in cast_names if n in edit["who"]]
            if isinstance(edit.get("what"), list):
                frame["what"] = [n for n in item_names if n in edit["what"]]
        in_frame(current["first"], shots.get("first") or {})
        current["first"]["text"] = str((shots.get("first") or {}).get("text", current["first"]["text"]))[:1500]
        current["first"]["locked"] = bool((shots.get("first") or {}).get("locked", current["first"].get("locked")))
        edits = {e.get("segment_id"): e for e in shots.get("ends", []) if isinstance(e, dict)}
        for end in current["ends"]:
            edit = edits.get(end.get("segment_id"))
            if edit:
                end["text"] = str(edit.get("text", end["text"]))[:1500]
                if edit.get("start") in ("new", "continue"):
                    end["start"] = edit["start"]
                end.setdefault("begin", {"text": "", "who": []})
                end["begin"]["text"] = str((edit.get("begin") or {}).get("text", end["begin"].get("text", "")))[:1500]
                in_frame(end, edit)
                in_frame(end["begin"], edit.get("begin") or {})
                end["locked"] = bool(edit.get("locked", end.get("locked")))
                end["begin"]["locked"] = bool((edit.get("begin") or {}).get("locked", end["begin"].get("locked")))
        return update_assist(pid, shots=current)


def shot_references(shot, cast, items):
    """Who and what one key frame sends to the image model: (cast members with a picture, the reference pictures in order,
    assets shown by picture, assets described in words)."""
    people = [cast[n] for n in shot.get("who", []) if n in cast and cast[n]["views"]][:4]
    refs = [next((v for v in c["views"] if "waist" in (v.get("label") or "").lower()), c["views"][0])["image"] for c in people]
    # Assets and props take the reference slots the cast leaves free (FLUX.2 has four), so a frame with nobody in it
    # can show up to four of them. One that does not fit, or has no picture yet, is described in words instead.
    named = [items[n] for n in shot.get("what", []) if n in items]
    shown = [t for t in named if t.get("views")][:max(0, 4 - len(people))]
    refs += [t["views"][0]["image"] for t in shown]
    told = [t for t in named if t not in shown]
    return people, refs, shown, told


def assist_shot_lengths(pid):
    """For each key frame, how many characters are sent on top of its own description: the sentences for its ticked cast
    and assets, the visual style and the closing line. The page adds the description's own length for the count it shows."""
    project = load_project(pid)
    shots = (project.get("assist") or {}).get("shots") or {}
    style = (approved_story(project) or {}).get("style", "") if (project.get("assist") or {}).get("story") else ""
    cast = {c["name"]: c for c in cast_members(project)}
    items = {t["name"]: t for t in stage_items(project)}

    def added(shot):
        people, _, shown, told = shot_references(shot or {}, cast, items)
        return len(assist.shot_prompt("X", style, people, shown, told)) - len("X.")
    frames = {}
    if shots.get("first"):
        frames["first"] = added(shots["first"])
    for e in shots.get("ends", []):
        frames[e.get("segment_id")] = added(e)
        frames["begin:" + str(e.get("segment_id"))] = added(e.get("begin"))
    return {"frames": frames}


def assist_shot_images(pid, targets):
    """Queue key frame images. `targets` lists "first", "begin:<segment id>" and segment ids (end frames); None means every frame."""
    with LOCK:
        project = load_project(pid)
        story = approved_story(project)
        shots = (project.get("assist") or {}).get("shots")
        if not shots:
            raise ValueError("Write the shots first.")
        cast = {c["name"]: c for c in cast_members(project)}
        items = {t["name"]: t for t in stage_items(project)}
        segments = {s["id"]: s for s in project["segments"]}
        if not project["segments"]:
            raise ValueError("There are no segments on the storyboard.")
        plan = [("first", project["segments"][0], "begin_image", shots["first"], "Opening frame")]
        for i, e in enumerate(shots["ends"]):
            seg = segments.get(e.get("segment_id"))
            if seg and seg is not project["segments"][0] and e.get("start") == "new":      # a new scene opens on its own picture
                plan.append(("begin:" + seg["id"], seg, "begin_image", e.get("begin") or {}, f"{seg.get('name') or 'Segment'} begin"))
            plan.append((e.get("segment_id"), seg, "end_image", e, None))
        busy = {(j.get("segment_id"), j.get("slot")) for j in project["image_jobs"] if j.get("kind") == "shot" and j.get("status") in PENDING}
        threading.Thread(target=assist.unload, daemon=True).start()
        made = []
        for key, seg, slot, shot, label in plan:
            if seg and slot == "end_image" and seg.get("type") == "singing":
                continue                    # singing clips have no end shot
            if targets is None and shot.get("locked"):
                continue                    # locked pictures are kept when everything else is regenerated
            if not seg or not (shot.get("text") or "").strip() or (targets is not None and key not in targets) or (seg["id"], slot) in busy:
                continue
            people, refs, shown, told = shot_references(shot, cast, items)
            label = label or f"{seg.get('name') or 'Segment'} end"
            project["image_jobs"] = [j for j in project["image_jobs"] if not (j.get("kind") == "shot" and j.get("status") == "error"
                                                                           and j.get("segment_id") == seg["id"] and j.get("slot") == slot)]
            made.append(queue_still(project, assist.shot_prompt(shot["text"], story.get("style", ""), people, shown, told), refs,
                                    workflows.safe_name(label), kind="shot", segment_id=seg["id"], slot=slot, label=label))
        if not made:
            raise ValueError("Nothing to generate: those frames are already being made, or have no description.")
        return {"jobs": made}


# ---------- system status ----------

def cpu_percent(interval=0.25):
    def snap():
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return idle, sum(parts)
    try:
        idle1, total1 = snap()
        time.sleep(interval)
        idle2, total2 = snap()
        span = total2 - total1
        return round(100.0 * (1 - (idle2 - idle1) / span), 1) if span else 0.0
    except (OSError, ValueError, IndexError):
        return None


def memory_info():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024
        total, free = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - free, "percent": round(100.0 * (total - free) / total, 1)}
    except (OSError, ValueError, KeyError, IndexError):
        return None


def gpu_info():
    """GPU load from nvidia-smi. Returns None when the tool is not available in this container."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        name, util, temp, power = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")][:4]

        def num(value):
            try:
                return float(value)
            except ValueError:
                return None
        return {"name": name, "percent": num(util), "temperature": num(temp), "power": num(power)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def system_status():
    started = time.time()
    try:
        queue = comfy_json("/queue", timeout=5)
        comfy = {"ok": True, "ms": round((time.time() - started) * 1000),
                 "running": len(queue.get("queue_running", [])), "waiting": len(queue.get("queue_pending", []))}
    except OSError as e:
        comfy = {"ok": False, "error": str(e)}
    try:
        disk = shutil.disk_usage(ROOT)
        disk = {"total": disk.total, "used": disk.used, "percent": round(100.0 * disk.used / disk.total, 1)}
    except OSError:
        disk = None
    try:
        with open("/proc/loadavg") as f:
            load = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        load = None
    return {"comfy": comfy, "cpu": {"percent": cpu_percent(), "cores": os.cpu_count(), "load": load},
            "memory": memory_info(), "gpu": gpu_info(), "disk": disk, "time": time.time()}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def test_voice(self, payload):
        """Speak one short line with a speech model and send back the sound. Like every other use of the graphics
        card outside the render queue, it waits its turn: it is refused while a render is running or waiting."""
        try:
            if not usage._comfy_idle(comfy_json):
                raise ValueError("A render is running or waiting. Test the voice when the queue is empty, so the render is not slowed.")
        except OSError:
            pass                                             # ComfyUI not running: nothing to disturb
        reference = None
        if payload.get("audio"):
            try:
                reference = base64.b64decode(str(payload["audio"]).split(",", 1)[-1], validate=True)
            except ValueError:
                raise ValueError("That recording could not be read.")
            if len(reference) > 20 * 1024 * 1024:
                raise ValueError("That recording is too large. A clip of 5 to 15 seconds is enough.")
        wav, did = models.test_voice(payload.get("id"), reference, payload.get("text") or "")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.send_header("X-Voice-Test", urllib.parse.quote(did))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(wav)

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def foreign(self, path=""):
        """3D studio (Phase 24): refuse a change that another website asked this browser to make, and an upload that is
        too large before reading it. -> True if the request was refused and answered."""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_json({"error": "That request came from another site and was refused."}, 403)
            return True
        if int(self.headers.get("Content-Length") or 0) > studio3d_api.assets.MAX_IMPORT:
            self.send_json({"error": "That file is larger than the studio reads."}, 413)
            return True
        return False

    def identity(self, method, url, body=None):
        """Hand a /api/identity/ request to the Identity Studio module, which owns everything about it."""
        return self.module(identity_api, method, url, body)

    def module(self, api, method, url, body=None):
        """Hand a request to a self-contained module (Identity Studio, Audio Lab) and send back what it returns."""
        try:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if method == "POST" and api.wants_raw(url.path):                   # an uploaded file, not JSON
                reply = api.handle(method, url.path, query, raw=body)
            else:
                reply = api.handle(method, url.path, query, json.loads(body or b"{}") if body is not None else None)
            if isinstance(reply, api.File):
                with open(reply.path, "rb") as f:                              # sent in pieces: an exported mix can be very large
                    self.send_response(200)
                    self.send_header("Content-Type", reply.content_type)
                    self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    return shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            return self.send_json(reply)
        except json.JSONDecodeError:
            return self.send_json({"error": "The request was not valid JSON."}, 400)
        except LookupError:
            return self.send_json({"error": "Not found"}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        except OSError as e:
            return self.send_json({"error": f"The server could not read or write a file: {e}"}, 500)

    def proxy(self, method, path, body=None):
        """Pass a request through to ComfyUI, including Range so media can seek."""
        headers = {k: self.headers[k] for k in ("Content-Type", "Range") if self.headers.get(k)}
        req = urllib.request.Request(COMFY + path, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e
        except OSError as e:
            return self.send_json({"error": f"Cannot reach ComfyUI at {COMFY}: {e}"}, 502)
        data = resp.read()
        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
        for key in ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Disposition"):
            if resp.headers.get(key):
                self.send_header(key, resp.headers[key])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if identity_api.handles(url.path):
                return self.identity("GET", url)
            if audio_api.handles(url.path):
                return self.module(audio_api, "GET", url)
            if studio3d_api.handles(url.path):
                return self.module(studio3d_api, "GET", url)
            if url.path.startswith("/static/"):            # the SWIFT tokens, logo, fonts and the module scripts
                kinds = {".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".js": "text/javascript", ".png": "image/png"}
                base = os.path.join(ROOT, "static")
                path = os.path.realpath(os.path.join(base, urllib.parse.unquote(url.path[len("/static/"):])))
                kind = kinds.get(os.path.splitext(path)[1].lower())
                if not kind or not path.startswith(base + os.sep) or not os.path.isfile(path):
                    return self.send_error(404)
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                # Scripts and styles change with every update, so the browser must ask again each time. Fonts and logos do not.
                self.send_header("Cache-Control", "no-cache" if kind in ("text/javascript", "text/css") else "max-age=3600")
                self.end_headers()
                return self.wfile.write(body)
            if url.path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "static", "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/api/projects":
                return self.send_json({"projects": list_projects()})
            if url.path == "/api/all":
                return self.send_json({"projects": everything()})
            if url.path == "/api/project":
                return self.send_json(refresh_statuses(query.get("id", [""])[0]))
            if url.path == "/api/ping":
                return self.send_json({"ok": True, "time": time.time()})
            if url.path == "/api/system":
                return self.send_json(system_status())
            if url.path == "/api/config/info":
                return self.send_json(dict(config.options(), in_use=models_in_use()))
            if url.path == "/api/lore":
                return self.send_json(lore_listing())
            if url.path == "/api/prompt_limits":
                return self.send_json(workflows.PROMPT_LIMITS)
            if url.path == "/api/assist/shot_lengths":
                return self.send_json(assist_shot_lengths(urllib.parse.parse_qs(url.query).get("project", [""])[0]))
            if url.path == "/api/generator/library":
                return self.send_json(generator_library(urllib.parse.parse_qs(url.query).get("project", [""])[0]))
            if url.path == "/api/reference":
                return self.send_json(reference_view(query.get("project", [""])[0]))
            if url.path == "/api/generator":
                return self.send_json(generator_view(urllib.parse.parse_qs(url.query).get("project", [""])[0]))
            if url.path == "/api/system/usage":
                return self.send_json(usage.report())
            if url.path == "/api/models":
                return self.send_json(models.listing())
            if url.path == "/api/assist/status":
                return self.send_json(assist_status(query.get("project", [""])[0]))
            if url.path == "/api/assist/vocals":
                body = assist_vocals(query.get("project", [""])[0])
                if body is None:
                    return self.send_json({"error": "The separated voice is not kept any more. Analyze the song again with separation to hear it."}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/api/assist/health":
                try:
                    service = assist_service("/health", timeout=4)
                except ValueError as e:
                    service = {"ok": False, "error": str(e)}
                return self.send_json({"llm": assist.status(), "service": service})
            if url.path == "/api/health":
                try:
                    queue = comfy_json("/queue", timeout=5)
                    return self.send_json({"comfy": True, "comfy_port": COMFY_PORT,
                                           "running": len(queue.get("queue_running", [])),
                                           "waiting": len(queue.get("queue_pending", []))})
                except OSError as e:
                    return self.send_json({"comfy": False, "comfy_port": COMFY_PORT, "error": str(e)})
            if url.path == "/comfy/view":
                return self.proxy("GET", "/view?" + url.query)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 404)
        self.send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        url = urllib.parse.urlsplit(self.path)
        if identity_api.handles(url.path):
            return self.identity("PUT", url, self.read_body())
        if audio_api.handles(url.path):
            return self.module(audio_api, "PUT", url, self.read_body())
        if studio3d_api.handles(url.path):
            return self.foreign() or self.module(studio3d_api, "PUT", url, self.read_body())
        if url.path == "/api/project":
            try:
                pid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
                return self.send_json(merge_project(pid, json.loads(self.read_body() or b"{}")))
            except (ValueError, TypeError) as e:
                return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if studio3d_api.handles(path) and self.foreign(path):
            return
        body = self.read_body()
        if identity_api.handles(path):
            return self.identity("POST", urllib.parse.urlsplit(self.path), body)
        if audio_api.handles(path):
            return self.module(audio_api, "POST", urllib.parse.urlsplit(self.path), body)
        if studio3d_api.handles(path):
            return self.module(studio3d_api, "POST", urllib.parse.urlsplit(self.path), body)
        if path == "/api/upload":
            return self.proxy("POST", "/upload/image", body)
        try:
            payload = json.loads(body or b"{}")
            pid = payload.get("project")
            if path == "/api/models/check":
                return self.send_json(models.start_check())
            if path == "/api/models/token":
                return self.send_json(models.set_token(payload.get("token")))
            if path == "/api/models/forget_token":
                return self.send_json(models.forget_token())
            if path == "/api/models/update":
                return self.send_json(models.start_update(payload.get("id")))
            if path == "/api/reference/save":
                return self.send_json(reference_save(pid, payload.get("state")))
            if path == "/api/reference/run":
                return self.send_json(reference_run(pid, payload.get("reference"), payload.get("state")))
            if path == "/api/reference/views":
                return self.send_json(reference_views(pid, payload.get("reference"), payload.get("row"), payload.get("what"), payload.get("state"), bool(payload.get("preview"))))
            if path == "/api/reference/assemble":
                return self.send_json(reference_assemble(pid, payload.get("reference"), payload.get("png"), payload.get("used"), payload.get("state")))
            if path == "/api/reference/check":
                return self.send_json(reference_check(pid, payload.get("reference"), payload.get("row")))
            if path == "/api/reference/export":
                return self.send_json(reference_export(pid, payload.get("reference"), payload.get("profile")))
            if path == "/api/reference/send":
                return self.send_json(reference_send(pid, payload.get("reference"), payload.get("remove")))
            if path == "/api/reference/place":
                return self.send_json(reference_place(pid, payload.get("reference"), payload.get("row"), payload.get("direction"), payload.get("image"), payload.get("state")))
            if path == "/api/reference/decide":
                return self.send_json(reference_decide(pid, payload.get("ids"), payload.get("action"), payload.get("confirmed"), bool(payload.get("force"))))
            if path == "/api/reference/extract":
                return self.send_json(reference_extract(pid, payload.get("reference"), payload.get("source"), payload.get("cuts")))
            if path == "/api/generator/save":
                return self.send_json(generator_save(pid, payload.get("state")))
            if path == "/api/generator/run":
                return self.send_json(generator_run(pid, payload.get("box"), payload.get("state"), payload.get("placed")))
            if path == "/api/generator/combine":
                return self.send_json(generator_combine(pid, payload.get("text"), payload.get("references")))
            if path == "/api/generator/describe":
                return self.send_json(generator_describe(pid, payload.get("image"), payload.get("use")))
            if path == "/api/generator/reset":
                return self.send_json(generator_reset(pid, payload.get("mode")))
            if path == "/api/generator/apply":
                return self.send_json(generator_apply(pid, payload.get("layer"), payload.get("image")))
            if path == "/api/generator/remove":
                return self.send_json(generator_remove(pid, payload.get("id")))
            if path == "/api/models/test_voice":
                return self.test_voice(payload)
            if path == "/api/projects":
                return self.send_json(save_project(empty_project(str(payload.get("name") or "Untitled project")[:80])))
            if path == "/api/projects/duplicate":
                with LOCK:
                    copy = load_project(payload.get("id"))
                    copy.update(id=new_id(), name=(copy["name"] + " copy")[:80], created=time.time())
                    for seg in copy["segments"]:          # the copy keeps finished clips, drops unfinished jobs
                        seg["results"] = [r for r in seg.get("results", []) if r.get("status") == "done"]
                    return self.send_json(save_project(copy))
            if path == "/api/projects/delete":
                with LOCK:
                    os.makedirs(TRASH_DIR, exist_ok=True)
                    src = project_path(payload.get("id"))
                    if not os.path.exists(src):
                        raise ValueError("That project no longer exists.")
                    os.replace(src, os.path.join(TRASH_DIR, f"{payload['id']}-{int(time.time())}.json"))
                return self.send_json({"ok": True})
            if path == "/api/actor/describe":
                return self.send_json(actor_describe(pid, payload.get("actor_id")))
            if path == "/api/actor/generate":
                return self.send_json(actor_generate(pid, payload.get("actor_id"), payload.get("refs"), payload.get("views"), payload.get("extra")))
            if path == "/api/image/fix":
                return self.send_json(image_fix(pid, payload.get("image"), payload.get("mask"), payload.get("prompt"),
                                                payload.get("width"), payload.get("height"), payload.get("label"), payload.get("refs")))
            if path == "/api/generated/remove":
                return self.send_json(generated_remove(pid, payload.get("id")))
            if path == "/api/image_jobs/clear":
                return self.send_json(image_jobs_clear(pid, payload.get("ids")))
            if path == "/api/assist/analyze":
                return self.send_json(assist_analyze(pid, payload.get("separate") is True))
            if path == "/api/assist/lyrics":
                lines = [{"start": float(l["start"]), "end": float(l["end"]), "text": str(l["text"])[:300]}
                         for l in payload.get("lines", []) if str(l.get("text", "")).strip()]
                return self.send_json(update_assist(pid, lines=lines))
            if path == "/api/assist/align":
                data = assist_data(load_project(pid))
                if not data.get("lines"):
                    raise ValueError("There is no transcription to match the lyrics against.")
                return self.send_json(update_assist(pid, lines=assist.align_lyrics(data["lines"], str(payload.get("text", "")))))
            if path == "/api/assist/split":
                return self.send_json(assist_split(pid))
            if path == "/api/assist/apply_split":
                return self.send_json(assist_apply_split(pid))
            if path == "/api/assist/story":
                return self.send_json(assist_story(pid, payload.get("direction"), payload.get("style"), bool(payload.get("another"))))
            if path == "/api/config/defaults":
                config.save_defaults(payload.get("config"))
                return self.send_json({"ok": True})
            if path == "/api/assist/story_beat":
                return self.send_json(assist_story_beat(pid, payload.get("segment_id"), payload.get("text"), payload.get("locked")))
            if path == "/api/lore/save":
                lore.save(payload.get("id") or "", payload.get("title"), payload.get("text"))
                return self.send_json(lore_listing())
            if path == "/api/lore/assets":
                lore.save_assets(payload.get("id") or "", payload.get("assets"))
                return self.send_json({"ok": True})
            if path == "/api/thing/generate":
                return self.send_json(thing_generate(pid, payload.get("thing_id"), payload.get("lore_id"), payload.get("refs"), payload.get("views"), payload.get("extra", "")))
            if path == "/api/lore/delete":
                lore.delete(payload.get("id") or "")
                return self.send_json(lore_listing())
            if path == "/api/lore/tag":
                return self.send_json(lore_tag(payload.get("id") or "", pid, payload.get("on") is True))
            if path == "/api/assist/approve_story":
                return self.send_json(assist_approve_story(pid, bool(payload.get("fill_prompts", True))))
            if path == "/api/assist/cast":
                return self.send_json(assist_cast_save(pid, payload.get("cast")))
            if path == "/api/assist/recommend_cast":
                return self.send_json(assist_recommend_cast(pid))
            if path == "/api/assist/shots":
                return self.send_json(assist_shots(pid))
            if path == "/api/assist/shots_save":
                return self.send_json(assist_shots_save(pid, payload.get("shots")))
            if path == "/api/assist/shot_images":
                return self.send_json(assist_shot_images(pid, payload.get("targets")))
            if path == "/api/render":
                return self.send_json({"results": render_segment(pid, payload.get("segment_id"), payload.get("runs"))})
            if path == "/api/render_all":
                return self.send_json(render_all(pid))
            if path == "/api/result/delete":
                with LOCK:
                    project = load_project(pid)
                    for seg in project["segments"]:
                        if seg["id"] == payload.get("segment_id"):
                            seg["results"] = [r for r in seg.get("results", []) if r["id"] != payload.get("result_id")]
                            if seg.get("chosen") == payload.get("result_id"):
                                seg["chosen"] = None
                    save_project(project)
                return self.send_json(project)
            if path == "/api/system/release":
                return self.send_json(usage.release_for_view(comfy_json, payload.get("view"), llm_busy=assist.active > 0))
            if path == "/api/system/free":
                return self.send_json(usage.free(comfy_json))
            if path == "/api/cancel":
                comfy_json("/interrupt", {})
                comfy_json("/queue", {"clear": True})
                if pid:
                    cancel_waiting(pid)
                return self.send_json({"ok": True})
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except OSError as e:
            return self.send_json({"error": f"Cannot reach ComfyUI at {COMFY}: {e}"}, 502)
        self.send_json({"error": "Not found"}, 404)


if __name__ == "__main__":
    migrate_legacy()
    threading.Thread(target=reference_sweep, daemon=True).start()
    # Identity Studio draws with the same FLUX.2 graphs as the storyboard, so there is one configuration to keep.
    identity_api.configure(build_image=workflows.build_image, build_fix=workflows.build_fix, before=assist.unload)
    identity_api.start()
    assist.before_chat = make_room_for_language_model
    audio_api.configure(project_audio=project_audio_files)
    studio3d_api.configure(resolve_reference=studio3d_reference, read_reference=studio3d_picture, handovers=studio3d_handovers, project_name=studio3d_project_name)
    studio3d_api.start()
    print(f"SWIFT Director on http://0.0.0.0:{PORT}  ->  ComfyUI at {COMFY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
