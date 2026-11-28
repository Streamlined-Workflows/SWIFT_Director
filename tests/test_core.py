"""Regression tests for SWIFT Director's core: what must keep working while Task 02 changes things around it.

Run from the repository root:  python3 -m unittest discover -s tests -t .
No ComfyUI, Ollama or network is needed; nothing here leaves the test's own temporary folder.
"""
import json
import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name                       # must be set before the app is imported

import app            # noqa: E402
import assist         # noqa: E402
import lore           # noqa: E402
import models         # noqa: E402
import workflows      # noqa: E402


def linked(graph):
    """Every link in a ComfyUI graph points at a node that exists."""
    return all(v[0] in graph for n in graph.values() for v in n["inputs"].values() if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str))


def kinds(graph):
    return {n["class_type"] for n in graph.values()}


SEG = {"id": "s1", "name": "Verse", "start": 10.0, "end": 16.0, "type": "story", "begin_image": "a.png", "end_image": None, "prompt": "She runs."}
SONG = {"filename": "song.wav", "duration": 180}


class ProjectTests(unittest.TestCase):
    def new(self, **fields):
        p = app.empty_project("Test")
        p.update(fields)
        return app.save_project(app.normalize(p))

    def test_a_project_from_before_any_of_this_still_opens(self):
        """The oldest shape we can meet: a name and segments, nothing else."""
        old = {"id": app.new_id(), "name": "Old one", "segments": [dict(SEG, results=[{"status": "done", "files": []}])]}
        os.makedirs(app.PROJECTS_DIR, exist_ok=True)
        with open(app.project_path(old["id"]), "w") as f:
            json.dump(old, f)
        p = app.load_project(old["id"])
        for key in ("settings", "actors", "audio", "photos", "image_jobs", "generated", "lore", "things"):
            self.assertIn(key, p, key)
        self.assertEqual(p["segments"][0]["results"][0]["status"], "done")          # nothing it had was dropped
        self.assertEqual(p["name"], "Old one")

    def test_unknown_fields_survive_a_load_and_save(self):
        """Fields a newer version adds must not be stripped by this one."""
        p = self.new(from_the_future={"x": 1})
        app.save_project(app.load_project(p["id"]))
        self.assertEqual(app.load_project(p["id"])["from_the_future"], {"x": 1})

    def test_a_browser_save_cannot_touch_what_the_server_owns(self):
        p = self.new(segments=[dict(SEG, results=[{"id": "r1", "status": "done"}])], lore=["abc12345"], assist={"story": {"logline": "L"}})
        app.merge_project(p["id"], {"name": "Renamed", "lore": [], "assist": {}, "generated": ["x"],
                                    "segments": [dict(SEG, prompt="New words", results=[], runs=99)]})
        after = app.load_project(p["id"])
        self.assertEqual((after["name"], after["segments"][0]["prompt"]), ("Renamed", "New words"))
        self.assertEqual(after["segments"][0]["results"], [{"id": "r1", "status": "done"}])    # render results kept
        self.assertEqual(after["segments"][0]["runs"], 8)                                      # clamped
        self.assertEqual((after["lore"], after["assist"]), (["abc12345"], {"story": {"logline": "L"}}))

    def test_a_singing_segment_never_keeps_an_end_shot(self):
        p = self.new(segments=[dict(SEG)])
        app.merge_project(p["id"], {"segments": [dict(SEG, type="singing", end_image="end.png")]})
        self.assertIsNone(app.load_project(p["id"])["segments"][0]["end_image"])

    def test_assets_of_every_kind_are_saved_from_the_browser(self):
        p = self.new()
        app.merge_project(p["id"], {"segments": [], "actors": [{"id": "a", "name": "Misty", "views": []}],
                                    "things": [{"id": "t", "kind": "prop", "name": "Sidearm", "views": []}], "photos": [{"id": "p", "name": "x", "image": "x.png"}]})
        after = app.load_project(p["id"])
        self.assertEqual((len(after["actors"]), len(after["things"]), len(after["photos"])), (1, 1, 1))

    def test_bad_project_ids_are_refused(self):
        for bad in ("../x", "", "a/b"):
            with self.assertRaises(ValueError):
                app.load_project(bad)

    def test_listing_counts_and_skips_damaged_files(self):
        p = self.new(segments=[dict(SEG)])
        with open(os.path.join(app.PROJECTS_DIR, "broken.json"), "w") as f:
            f.write("{not json")
        ids = [x["id"] for x in app.list_projects()]
        self.assertIn(p["id"], ids)
        self.assertNotIn("broken", ids)


class GraphTests(unittest.TestCase):
    """The ComfyUI graphs: each engine still builds, is fully wired, and uses the models it should."""

    def test_which_engine_renders_a_segment(self):
        self.assertEqual(workflows.engine(SEG), "story")
        self.assertEqual(workflows.engine(dict(SEG, end_image="e.png")), "story to end shot")
        self.assertEqual(workflows.engine(dict(SEG, type="singing", end_image="e.png")), "singing")

    def test_every_engine_builds_a_wired_graph_that_saves_a_video(self):
        for seg in (SEG, dict(SEG, end_image="e.png"), dict(SEG, type="singing")):
            g = workflows.build(seg, SONG, 7, prefix="storyboard/x/clip")
            self.assertTrue(linked(g), workflows.engine(seg))
            self.assertTrue(any("Video" in k or "VHS" in k for k in kinds(g)), kinds(g))
            self.assertIn("SaveImage", kinds(g))                                    # the last frame, for continuing shots

    def test_a_segment_needs_a_begin_shot_and_a_positive_length(self):
        for bad in (dict(SEG, begin_image=None), dict(SEG, end=10.0)):
            with self.assertRaises(ValueError):
                workflows.build(bad, SONG, 1)

    def test_stills_and_fixes(self):
        g = workflows.build_image("a ship", ["r1.png", "r2.png", "r3.png", "r4.png", "r5.png"], 3)
        self.assertTrue(linked(g))
        self.assertEqual(sum(n["class_type"] == "ReferenceLatent" for n in g.values()), 4)       # FLUX.2 takes four
        fix = workflows.build_fix("pic.png", "mask.png", "smooth it", 3, 1250, 730, refs=["prop.png"])
        self.assertTrue(linked(fix))
        self.assertTrue({"SetLatentNoiseMask", "ImageCompositeMasked"} <= kinds(fix))            # only the painted area changes
        self.assertEqual(sum(n["class_type"] == "ReferenceLatent" for n in fix.values()), 2)

    def test_model_file_names_are_all_in_the_update_catalog(self):
        """A model a graph loads but the AI Models panel does not list could never be checked or updated."""
        listed = json.dumps(models.CATALOG)
        for key, name in workflows.MODELS.items():
            self.assertIn(name, listed, key)

    def test_catalog_entries_are_complete_and_unique(self):
        ids = [m["id"] for m in models.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for m in models.CATALOG:
            self.assertTrue(m.get("name") and m.get("use") and m.get("group"), m["id"])


class AssistTests(unittest.TestCase):
    """The language-model requests, with the model itself replaced by a canned answer."""

    def setUp(self):
        self.real, self.seen = assist.chat, {}

        def fake(system, user, schema, **kw):
            self.seen.update(system=system, user=user, schema=schema)
            frame = {"description": "A frame.", "cast": ["misty", "Nobody"], "assets": ["the harrow"]}
            return {"title": "T", "logline": "L", "style": "S", "beats": ["one"], "characters": [{"name": "Pilot", "role": "r", "description": "d"}],
                    "first_frame": frame, "segments": [{"starts": "continue", "reason": "why", "begin_frame": {"description": "", "cast": [], "assets": []}, "end_frame": frame}] * 3}
        assist.chat = fake
        self.segs = [dict(SEG, id=str(i), start=i * 6.0, end=i * 6.0 + 6) for i in range(3)]
        self.analysis = {"tempo": 120, "duration": 18}

    def tearDown(self):
        assist.chat = self.real

    def test_story_has_one_line_per_segment_and_keeps_locked_lines(self):
        out = assist.write_story(self.analysis, [], self.segs, direction="darker", keep={2: "Locked ending."}, lore="## World\nTeal Accord.")
        self.assertEqual(len(out["beats"]), 3)                                       # padded when the model returns too few
        self.assertEqual(out["beats"][2], "Locked ending.")
        for part in ("darker", "Locked ending.", "Teal Accord."):
            self.assertIn(part, self.seen["user"])

    def test_shots_follow_the_rules_the_storyboard_depends_on(self):
        cast = [{"name": "Misty", "role": "pilot", "description": "teal uniform"}]
        out = assist.write_shots({"title": "t"}, self.analysis, [], self.segs, cast, things=[{"name": "The Harrow", "kind": "scene", "context": "her ship"}])
        self.assertEqual(len(out["ends"]), 3)
        self.assertEqual(out["ends"][0]["start"], "new")                             # the first segment always opens a scene
        self.assertEqual(out["ends"][1]["start"], "continue")
        self.assertEqual((out["first"]["who"], out["first"]["what"]), (["Misty"], ["The Harrow"]))   # only known names, in their own spelling

    def test_prompts_for_pictures(self):
        view = assist.view_prompt("red hair, teal uniform", "Close-up portrait", True, "wearing a helmet")
        self.assertLess(view.index("wearing a helmet"), view.index("teal uniform"))  # the one-off addition leads
        shot = assist.shot_prompt("She lands.", "3D", [{"name": "Misty", "description": "teal"}], [{"name": "The Harrow", "kind": "scene"}],
                                  [{"name": "Sidearm", "context": "issue pistol", "description": "matte black"}])
        for part in ("reference image 1", "The Harrow is the subject shown in reference image 2", "Sidearm: issue pistol. matte black."):
            self.assertIn(part, shot)
        self.assertEqual(len(assist.VIEW_FRAMINGS), 8)
        self.assertEqual(set(assist.THING_VIEWS), {"scene", "prop", "world"})

    def test_cuts_stay_inside_the_song_and_within_length_limits(self):
        beats = [i * 0.5 for i in range(361)]
        cuts = assist.propose_cuts({"duration": 180.0, "tempo": 120, "beats": beats, "downbeats": beats[::4], "sections": []}, [])
        self.assertGreater(len(cuts), 5)
        self.assertAlmostEqual(cuts[0]["start"], 0.0, places=1)
        self.assertAlmostEqual(cuts[-1]["end"], 180.0, places=1)
        for a, b in zip(cuts, cuts[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=3)                   # back to back, no gaps


class StoryboardRuleTests(unittest.TestCase):
    """Rules Jeremy set that later work must not undo."""

    def project(self):
        segs = [dict(SEG, id="a"), dict(SEG, id="b", start=16.0, end=22.0)]
        p = app.empty_project("Rules"); p["segments"] = segs
        draft = {"title": "T", "logline": "L", "style": "S", "beats": ["First.", "Second."], "segment_ids": ["a", "b"], "locks": []}
        p["assist"] = {"analysis": {"tempo": 120}, "story_draft": draft, "story": json.loads(json.dumps(draft))}
        p["segments"][0]["prompt"] = "First."
        return app.save_project(app.normalize(p))

    def test_a_locked_story_line_cannot_be_edited(self):
        p = self.project()
        app.assist_story_beat(p["id"], "a", locked=True)
        with self.assertRaises(ValueError):
            app.assist_story_beat(p["id"], "a", text="Changed.")
        app.assist_story_beat(p["id"], "a", locked=False)
        after = app.assist_story_beat(p["id"], "a", text="Changed.")
        self.assertEqual(after["assist"]["story"]["beats"][0], "Changed.")           # the approved copy follows
        self.assertEqual(after["segments"][0]["prompt"], "Changed.")                 # and so does a prompt copied from it

    def test_an_edited_prompt_is_not_overwritten_by_a_story_edit(self):
        p = self.project()
        app.merge_project(p["id"], {"segments": [dict(SEG, id="a", prompt="My own words."), dict(SEG, id="b", start=16.0, end=22.0)]})
        after = app.assist_story_beat(p["id"], "a", text="Changed.")
        self.assertEqual(after["segments"][0]["prompt"], "My own words.")

    def test_assets_are_ranked_scene_then_prop_then_world(self):
        entry = lore.save("", "The Verge", "Drowned stations.")
        lore.save_assets(entry["id"], [{"id": "w1", "kind": "world", "name": "Pact cruiser", "views": []}, {"id": "w2", "kind": "world", "name": "the harrow", "views": []}])
        p = app.empty_project("Rank"); p["lore"] = [entry["id"]]
        p["things"] = [{"id": "p1", "kind": "prop", "name": "Sidearm", "views": []}, {"id": "s1", "kind": "scene", "name": "The Harrow", "views": []}, {"id": "n", "kind": "scene", "name": " ", "views": []}]
        names = [(t["name"], t["kind"]) for t in app.stage_items(app.normalize(p))]
        self.assertEqual(names, [("The Harrow", "scene"), ("Sidearm", "prop"), ("Pact cruiser", "world")])   # the storyboard's own "Harrow" wins


class LoreTests(unittest.TestCase):
    def test_save_tag_text_budget_and_trash(self):
        a = lore.save("", "World", "Short text.")
        b = lore.save("", "Long", "word " * 5000)
        self.assertEqual(lore.load(a["id"])["title"], "World")
        text, cut = lore.for_model([a["id"], "ffffffffffff", b["id"]])               # a missing entry is skipped, not fatal
        self.assertTrue(cut)
        self.assertLessEqual(len(text), lore.BUDGET + 200)
        self.assertTrue(text.startswith("## World"))
        lore.save(a["id"], "World", "Edited.")                                        # editing the text keeps the world's assets
        lore.save_assets(a["id"], [{"id": "x", "kind": "prop", "name": "Mic", "views": [{"image": "m.png"}, {"nope": 1}]}, "junk"])
        lore.save(a["id"], "World 2", "Edited again.")
        kept = lore.load(a["id"])["assets"]
        self.assertEqual((len(kept), kept[0]["kind"], len(kept[0]["views"])), (1, "prop", 1))
        lore.delete(b["id"])
        with self.assertRaises(ValueError):
            lore.load(b["id"])
        self.assertTrue(any(n.startswith("lore-" + b["id"]) for n in os.listdir(os.path.join(_TMP.name, "trash"))))
        for bad in ("../x", "", None):
            with self.assertRaises(ValueError):
                lore.load(bad)




class ListenWithSeparation(unittest.TestCase):
    """Assisted Storyboard: the option to split the voice from the music before transcribing."""

    def test_the_choice_is_passed_on_and_a_refusal_says_why(self):
        sent, real_service, real_load, real_save = [], app.assist_service, app.load_project, app.save_project
        app.load_project = lambda pid: {"id": pid, "song": {"filename": "song.wav"}}
        app.save_project = lambda project, touch=True: project
        try:
            app.assist_service = lambda path, payload=None, timeout=15: sent.append((path, payload)) or {"state": "running"}
            app.assist_analyze("p1")
            app.assist_analyze("p1", separate=True)
            self.assertEqual([p["separate"] for _, p in sent], [False, True])
            self.assertEqual(sent[1][1]["key"], "p1:song.wav")

            def refusing(path, payload=None, timeout=15):
                if path == "/analyze":
                    raise ValueError("The song analysis service is not running on the server (HTTP Error 409).")
                return {"ok": True, "separation": False, "separation_reason": "The separation software is not installed in the song analysis service (ModuleNotFoundError)."}
            app.assist_service = refusing
            with self.assertRaises(ValueError) as e:
                app.assist_analyze("p1", separate=True)
            self.assertIn("separation software is not installed", str(e.exception))
        finally:
            app.assist_service, app.load_project, app.save_project = real_service, real_load, real_save



class LauncherManifest(unittest.TestCase):
    """What the desktop launcher reads to build its tabs."""

    def test_it_lists_the_places_to_open_and_the_server_only_services(self):
        m = app.launcher_manifest()
        by = {s["id"]: s for s in m["services"]}
        self.assertEqual((m["app"], by["director"]["port"], by["director"]["reach"], by["comfyui"]["reach"]), ("SWIFT Director", app.PORT, "tailscale", "tailscale"))
        self.assertEqual({s["reach"] for s in m["services"]}, {"tailscale", "server"})
        self.assertTrue(all(isinstance(s["port"], int) and s["name"] and s["summary"] and s["path"].startswith("/") for s in m["services"]))
        self.assertEqual({by[k]["reach"] for k in ("assist", "speech", "convert", "ollama")}, {"server"})

if __name__ == "__main__":
    unittest.main()
