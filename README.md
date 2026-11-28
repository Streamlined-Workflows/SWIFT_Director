# SWIFT Director

**Your story. Your vision. Directed by you, Built by AI.**

SWIFT Director is a browser studio for making music videos with AI models that run on your own machine. You bring the song and the characters and direct every shot; the models on a local ComfyUI server (built for an NVIDIA DGX Spark class machine) do the rendering. Nothing is sent to an outside service.

It has two ways of working. **Storyboard** is the hands-on workspace: split the song into segments and set each shot yourself. **Assisted Storyboard** has the AI listen to the song, propose the cuts, a story, a cast and the key frames for you to approve or change.

Load a song, split it into 5 to 15 second segments, give each one a begin shot, a prompt and a number of runs, and render the clips on your own server.

## Start it

On the server (the machine that runs ComfyUI):

```
git clone https://github.com/Streamlined-Workflows/SWIFT_Director ~/SWIFT_Director
~/SWIFT_Director/run.sh
```

Then open `http://<server-name>.local:8190` from any computer on the same network.

It runs as a Docker container named `swift-director` that starts on boot (with `swift-director-assist` beside it for song analysis). It expects ComfyUI on the same machine at port 8188.

## Update it

```
~/SWIFT_Director/update.sh
```

## What each part does

| File | Purpose |
|---|---|
| `app.py` | Web server (Python standard library only). Stores projects, forwards uploads and media to ComfyUI, queues renders, reports system load. |
| `workflows.py` | Builds the ComfyUI job for each segment. |
| `assist.py` | Assisted Storyboard: cut points, lyric matching and story prompts for the language model. |
| `assist-service/` | Song analysis container (lyrics and beats). Built and started by `run.sh`. |
| `launcher/` | SWIFT Director Launcher: a Windows program with a tab for each place reached over Tailscale. See `launcher/README.md`. |
| `static/index.html` | The page. |

## Sidebar

| Tab | What it holds |
|---|---|
| Storyboard | The song, timeline and segments of the open project |
| Projects | Every saved project. Open, rename, duplicate or delete one; each keeps its own timeline, assets and clips |
| Assets | The open project's actors, photos and audio. An actor is one character with a name, a description and any number of views (stills); photos are loose reference stills |
| Asset Generator | Make pictures outside the storyboard. **Image Generation**: one prompt and up to four reference pictures. **Depth Generation**: a background, middle ground and foreground made separately, one of each applied, then redrawn as one picture. Any result can be saved to Assets as a scene asset or prop |
| Library | Every video rendered in the open project, with download links |
| Audio Lab | A multi-track audio timeline for the open project: import sounds, arrange, trim and split clips, mix with effects, and export a finished file |
| Lore | Long background text about a setting (world, factions, people, technology, look). Each entry can be tagged to any number of storyboards |
| Identity Studio | A separate workspace for logos and app identities. See [Identity Studio](#identity-studio) |
| System status | Ping test, and CPU, GPU, memory and disk meters for the server. Under the meters, a list of what is using memory and the GPU: the models Ollama and ComfyUI have loaded, and the largest programs on the server |

Each actor panel has two buttons. **Describe from images** sends up to four of the actor's views to the language model (it must be vision-capable) and writes the description. **Generate views from description** queues a close-up, a waist-up and a full-body still with FLUX.2 [dev] and the 8-step turbo LoRA, using up to three existing views as references so the new stills match; finished stills are copied into ComfyUI's input folder and added to the actor.

Assets and Library each have an **All / Project** toggle. All also lists what other projects hold: actors, photos and audio can be added to the open project (a copy that points at the same image and audio files), and clips can be watched, downloaded, or their project opened.

Projects are stored as `data/projects/<id>.json`. Deleted projects are moved to `data/trash/`. The GPU meter reads `nvidia-smi`, which is why `run.sh` starts the container with `--gpus all`. The list of largest programs reads the server's process list, which is why `run.sh` also uses `--pid host`; without it the container can see only itself.

## Assisted Storyboard

A guided pass that turns a song into a planned storyboard. Each step waits for approval.

| Step | What it does | Runs on |
|---|---|---|
| Listen | Tempo, beats, bar lines and sections (librosa); lyrics with timestamps (faster-whisper, CPU). Lyrics can be corrected line by line, or pasted and matched to the timing. Each line has a Play button that plays just that part of the song. **Separate the voice first** (optional, Demucs on the CPU) splits the voice from the music and transcribes only the voice; tempo and beats still come from the full song, and the separated voice can then be chosen for the Play buttons. The first use downloads a model of about 80 MB. | `assist-service/` container, port 8191, local only |
| Split | Picks cut points on bar lines so every segment is 5 to 15 s, favouring section changes and lyric phrase starts and avoiding cuts through a sung line. The language model names the segments and may merge neighbours. | `assist.py` + Ollama |
| Story | Writes a title, logline, visual style and one beat per segment. Approving copies each beat into its segment's empty prompt. | Ollama |
| Cast, Shots | Not built yet. | |

The language model is reached through Ollama on the server (`OLLAMA_URL`, default `http://127.0.0.1:11434`; `OLLAMA_MODEL`, default `qwen3.6:35b`). It is unloaded when a render is queued so its memory goes back to the video models.

## Timing, progress and batch renders

- Segments run back to back. Each has its own 5 to 15 second length slider; changing one shifts the segments after it.
- A segment with no begin shot starts from the end shot of the segment before it.
- Clips show a live progress bar read from ComfyUI's websocket (port 8188 must be reachable from the browser).
- Picking a clip puts it on the timeline strip, and playing the song shows the picked clips in the preview, in sync.
- **Render all segments** queues every segment's listed runs for an unattended batch. The page can be closed while it runs.

## Shot types

| Type | Engine | Frame rate | Long segments |
|---|---|---|---|
| Story | Wan 2.1 image-to-video with the lightx2v 4-step LoRA | 16 fps | Chained: each 5 s pass starts from the last frame of the one before |
| Singing | Wan 2.1 InfiniteTalk, driven by that slice of the song | 25 fps | Chained: each pass continues from the previous frames |
| Story with an end shot | Wan 2.2 first-and-last-frame video (high-noise and low-noise models, 4-step LoRAs) | 16 fps | Chained: only the final pass is steered to the end shot |

## Models it expects in ComfyUI

`diffusion_models/Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors`, `loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors`, `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `vae/Wan2_1_VAE_bf16.safetensors`, `clip_vision/clip_vision_h.safetensors`, `model_patches/wan2.1_infiniteTalk_single_fp16.safetensors`, `audio_encoders/wav2vec2-chinese-base_fp16.safetensors`.

For story segments that have an end shot, also: `diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors`, `diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors`, `loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors`, `loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors`, `vae/wan_2.1_vae.safetensors` (all from `Comfy-Org/Wan_2.2_ComfyUI_Repackaged`).

File names are set at the top of `workflows.py`.

## Not built yet

- Singing segments cannot be steered to an end shot (the lip-sync model takes a begin shot only).
- Generating new actor stills from a character reference.
- Upscaling and final assembly of picked clips into one video.

## Security

There is no login. Keep ports 8190 and 8188 on your home network only; do not forward them from your router.

## Audio patch for NVIDIA's ComfyUI container

NVIDIA's ComfyUI image for DGX Spark ships a placeholder in place of `torchaudio`, so any node that processes audio fails. `patch-comfy-audio.sh` copies two small pure-PyTorch files from `comfy-patches/` into the container (`resample` and `MelSpectrogram`, checked against the real library) and restarts ComfyUI. Run it again if the container is ever rebuilt.

## AI models panel

System status > AI models lists every model the storyboard uses, with what it does here. **Check for updates** compares each one with the version published today:

- Files in ComfyUI's models folder are compared by SHA-256 with the same file on Hugging Face. The first check reads every file once (several minutes); results are cached in `data/model_hashes.json`, so later checks are quick.
- The Ollama model is compared with the Ollama library; the speech model with its Hugging Face repo.

**Update** downloads the new file beside the old one, verifies it, then swaps it in and asks ComfyUI to unload the old copy. `run.sh` mounts the models folder (`COMFY_MODELS` overrides the path) and `~/.cache/huggingface` (read-only, for the token saved by `hf auth login`, needed for FLUX.2 and LTX-2.5). The list lives in `models.py`.

## Assisted Storyboard: cast and shots

After a story is approved, step 4 (Cast) lets you tick which actors appear and what each plays, or ask the language model to recommend characters (each can be added as an actor with its description). Step 5 (Shots) writes the opening frame and one end frame per segment from that segment's lyrics, length, energy and story beat; the descriptions are editable. Generating the images runs FLUX.2 with one picture of each cast member in frame as a reference, then sets the opening frame as segment 1's begin shot and each end frame as its segment's end shot (they are also added to Photos).

## How a segment begins

- **New angle**: the segment has its own begin shot and starts on it.
- **Continue**: the begin shot is empty. The segment starts from the previous segment's end shot if it has one; otherwise from the last frame of the clip picked on the previous segment. Until a clip is picked there, renders of this segment are created "on hold" and start by themselves once you pick. Every clip saves its final frame for this.
- Singing segments cannot have an end shot (the lip sync engine only takes a start image).

In Assisted Storyboard > Shots the language model also decides, for every segment after the first, whether it **continues** the previous shot or cuts to a **new scene** (place, subject or time changes, or the music turns sharply), with a one-line reason. New scenes get their own begin-frame description and image; continuing segments have their begin shot cleared. Each decision can be flipped by hand. **Render all** skips segments that would have to wait for a pick and lists them.

## Fixing part of a picture

Click a picture (Assets, Library > Images, Assisted Storyboard key frames) to open the viewer, then **Fix an area**: paint over the problem, describe what should be there, and Generate fix. FLUX.2 repaints only the painted area (`workflows.build_fix`: noise-masked sampling with the original as reference, then pasted back through the mask). The result is saved as a new picture; the original is kept.

**Hugging Face key.** If the server has no key, Check for updates first asks for a read-only Hugging Face token. It is verified with Hugging Face, then stored in `data/hf_token` (owner-only, never committed, never sent back to the browser). Skip checks everything else; models that need access show a yellow triangle and are not attempted. A key saved by `hf auth login` on the server is used automatically.

## Project configuration

Assisted Storyboard opens with the **Project Configuration Center**: six collapsible sections of
settings stored with the project (`config` in the project file, schema 2) and restored when it is
reopened. `config.py` owns the choices, the defaults and the checking of anything a browser sends;
`static/config/` draws it.

Each setting is tagged **In use** or **Stored**. Two are in use today: the music-video subtype
shapes the story concept, and Storyboard Detail sets how much each key frame description says.
The rest are saved for the update that will act on them, including the project rating, which is
a label for filtering rules to be applied against later. The model roles show what performs each
job today; choosing between models is the next milestone.

Switching the video type never deletes the other types' settings. Projects from before the
configuration existed open with defaults that reproduce their old behaviour, and their files are
not rewritten until they are next saved. **Use these settings for new projects** saves the
current configuration to `data/defaults.json`.

Tests: `./run-tests.sh` (Python only; no ComfyUI, Ollama or network).

## Memory

The CPU and GPU share one pool of memory on this machine, and ComfyUI keeps its last models
loaded while idle. SWIFT Director gives memory back in three ways:

- **On opening a tab.** A few seconds after a tab is opened, the model servers it does not use
  are unloaded: Storyboard and Identity Studio keep ComfyUI; Assisted Storyboard and Assets keep
  both; Projects, Library and Lore keep neither. System status changes nothing.
- **Before the language model writes**, ComfyUI is asked to release its models; and before
  ComfyUI renders or draws, the language model is unloaded.
- **Free memory** on System status unloads everything.

ComfyUI is only ever asked while its queue is empty, and the language model is never unloaded in
the middle of an answer. The cost is time: the next job reloads its model.

## Scene assets, props and world assets

Three kinds of asset sit beside actors in the Assets tab. Each has a name, a one-line context
(what it is and whose), a description, and any number of pictures, which can be uploaded or
generated from the description.

| Kind | Stored | Example |
|---|---|---|
| Scene asset | In the storyboard (`things`) | The one ship this video follows |
| Prop | In the storyboard (`things`) | A sidearm, a console, a drum kit, a stage |
| World asset, world prop | With a lore entry (`assets` in `data/lore/<id>.json`) | Thirty ships with their factions |

A world is a lore entry: every storyboard tagged with that lore sees its world assets.

When shots are planned, the language model is given each asset's name and context line only and
names the ones visible in each frame ("Assets and props" under the frame). When a frame's picture
is made, the cast takes FLUX.2's four reference slots first and assets fill what is left, in the
order scene assets, props, world assets; so a frame with nobody in it can show up to four. An
asset that does not fit, or has no picture yet, is described in words in the prompt instead.

**Fix an area** has a "Use:" list of assets and props that have a picture. Choosing one adds its
first picture as an extra reference for the repaint, which is how a prop is swapped into a shot
that is otherwise right.

## Audio Lab

A multi-track timeline for arranging a project's audio. It is a separate module (`audio/` and
`static/audio/`) with its own storage under `data/audio/` and its own routes under `/api/audio/`.

- **Sounds** are kept once in a shared library and never changed. A clip on the timeline points
  at a sound and carries instructions: where it sits, where in the sound it starts, how long it plays.
- **Time is counted in whole samples** at 48 kHz, so clips that should meet exactly do.
- **Waveforms** are drawn from peak data the server makes once per sound.
- **Each project's timeline is its own file**, saved atomically with the last 20 versions kept.
  A save from a second window is detected, not silently overwritten.

- **Clips** carry a level, fades, speed, pitch, reverse and a chain of effects. **Tracks** and the
  **master** carry volume and effects; tracks also balance, mute and solo. The effects are EQ,
  compressor and reverb.
- **The mixer** shows a meter per track and for the mix, and marks when the mix goes over full level.
- **Export** writes a new file with ffmpeg from the saved timeline: the full mix, each track as its
  own file (a zip), or part of the timeline; WAV, FLAC or MP3; 48 or 44.1 kHz. Nothing is
  normalised or limited: the loudest point is measured and reported. Exports run in the background
  on the processor and are kept under `data/audio/exports/`.
- **Playback in the browser is a preview.** The export is the finished result.

See `docs/Task_03_Milestone_C.md` for how the export is checked. The application's container
includes ffmpeg for this (`Dockerfile`).

## Speech service

`speech-service/` is a third container (port 8192, local only) holding the software for two speech
generation models: Qwen3-TTS (Apache 2.0) and Ming-3.0-tts (MIT). `run.sh` builds it last and carries
on if the build fails. Model files are not in the image; each is downloaded from System status, AI
Models, Speech generation, into the Docker volume `swift-director-speech-models`.

One model is loaded at a time, each in its own process, so unloading always returns the memory. A
model is released when another tab is opened, by Free memory, or after five idle minutes. **Test
voice** speaks one line so a model can be heard; it is refused while a render is running or waiting.
See `docs/Task_02_Milestone_2_Audio.md`.

## Text boxes

The boxes that hold prompts and descriptions are sized to their text: each is made exactly tall enough whenever its text
changes (typed, applied or rewritten) and when its tab opens, so nothing is hidden behind a scrollbar. A box can still be
dragged taller or shorter by its corner; that size lasts until its text next changes. Covered: segment video prompts,
story concept lines, Shots descriptions, actor and asset descriptions, and the Asset Generator's prompts. The Lore text
and the lyrics paste box are left at a fixed height because they can hold pages of text.

## Prompt counts

Every prompt box that feeds a model shows `x / y` at its bottom right: the characters that will be sent against about how
many the model reads. The limits live in one place, `workflows.PROMPT_LIMITS`: FLUX.2 [dev] for stills and Wan 2.1 / 2.2
for clips each read 512 tokens, shown as about 2,000 characters of English. The count warns near and past the limit and
never blocks. It covers the segment video prompts (sent as typed), the Shots descriptions (the server reports what each
frame adds for its ticked cast and assets, the style and the closing line), and the Asset Generator's prompts.

## Asset Generator

`generator.py` and `static/generator/`. What is stored per project (`project["generator"]`) is only what was typed and
chosen: each box's prompt, references and count, which picture is showing, and which three pictures are applied to the
composed output. The pictures are ordinary FLUX.2 stills marked `kind: "gen"` with the box they belong to.

- Each mode (Image, Character, Depth, 3D) opens with an **Asset Configuration** box, stored per mode in
  `project["generator"]["config"]`: Project Identity (asset name, rating, asset type), AI Model Configuration (prompting,
  image, image LoRA, background removal, upscale) and Workflow and Approval Controls. It is drawn with the Project Configuration
  Center's own markup and styles (`.cc`), so both boxes look and behave alike: sections that open and close with a one-line
  summary, and an In use or Stored tag on each setting. In use today: the name and type (what **Save to Assets** creates: an
  actor, a scene asset, a prop, or a world asset of a tagged Lore entry), asking before a run, and requiring approval
  before a picture can be saved. The rating and the model choices are stored; one model per role is installed.
- Each prompt box shows `x / y` at its bottom right: the characters that will be sent (typed text plus the sentences added
  for references) against about 2,000. FLUX.2 reads up to 512 tokens, which is roughly that many characters of English,
  so the figure is an estimate and is labelled as one. It warns near and past the limit; it does not block.
- While a box works, an empty picture area reads "Generating 1/3 photos" (which one it is on, of how many); once a
  picture is showing, the line under it reads "Generating an additional 2 images".
- **Use as Reference** on a generated picture adds it to a box's reference slots as Inspiration (the loosest use), without
  saving anything to Assets. In Depth Generation it asks which box.
- **Reset Image Generator** / **Reset Depth Generator**, at the bottom of each mode, puts that mode back to how a new
  project has it: prompts, references, counts, what is applied, and its Asset Configuration. The pictures it made leave the
  boxes but are not deleted; they stay in the Library. It is refused while that mode is still generating.
- An empty reference slot asks **Upload** or **Apply from Library**. The library lists every picture the project already
  has (generated pictures newest first, actors' views, assets and props, photos, storyboard shots), each once, with a name
  filter. Choosing one uses the existing file; nothing is copied.
- Every reference picture has a **How to use it** choice. Only **Follow closely (redrawn)** sends the picture to the
  image model, with one sentence added after the prompt. **Style guide** and **Inspiration** never send the picture: an
  attached image outweighs any wording, so the model copies it. Instead **Apply** has the language model (vision) describe
  the picture's look, or its general idea, in at most 300 characters, and puts that at the start of the prompt. Until
  Apply is pressed such a reference has no effect.
- **Apply again** replaces that reference's description. If the person typed inside it, a Yes / No question asks whether
  to keep those words (Yes leaves them in the prompt, No removes them with the old description). Text outside the
  description is never touched, and with nothing typed inside there is no question.
- Each reference keeps a number from 1 to 4 while it is there. Everything the language model wrote is shown in one
  colour, the `--swift-ai-text` token (set beside the other aliases in `static/index.html`; green in the SWIFT theme):
  the text under a reference, the words put in the prompt, and the Reference Prompt box. Those words are tracked as
  ranges of the prompt (`marks`); anything typed or changed, even inside one, falls outside the ranges and shows in the
  normal colour. The prompt box is a plain text area with see-through text over a coloured copy of the same text.
- **Reference Prompt** is a text box beside the reference slots. Each Apply adds that reference's description to it, and
  it can be edited by hand. **Clean and Combine** sends the box to the Prompting Model (`assist.combine_references`,
  `POST /api/generator/combine`), which rewrites it as one passage with the overlap removed, up to 300 characters for
  each applied style guide or inspiration reference. **Replace Prompt Inserts** removes the reference descriptions
  already in the prompt and puts the Reference Prompt text at the start in their place (asking the same Yes / No
  question if the person typed inside them).
- **3D Generation** is its own workspace, described under [3D Generation](#3d-generation) below.

## 3D Generation

The fourth mode of the Asset Generator. It is the first working slice ("Gate B") of the plan in `3d_Generation_Plan/`;
`docs/3D_Gate_B_Status.md` lists what is built, what is not, and what has only been tested against stand-ins.

What it does today: takes in a **binary FBX** file, a **GLB** file or a **SWIFT ZIP** it exported earlier, shows it in a 3D view, measures
it, checks it for faults, lets you name the parts and give them roles, checks a character's preparation, attaches reference
pictures sent from Reference Generation, and exports a SWIFT ZIP that can be brought back in.

What it does not do: make or change a 3D model. Generation, optimisation, UV unwrapping, textures, materials, pose
conversion, weight transfer and landmarks have no tool behind them on this computer, so their tabs say so and change nothing.
FBX files are stored exactly as they arrive: nothing is converted, repaired or re-saved. Text-form (ASCII) FBX is refused.

- **Pending and approved.** Every file that arrives is *pending* and is deleted 10 days later unless approved. Selecting,
  exporting, downloading and recording a hand-off never approve anything or give more time. **Approve on import** keeps a
  file straight away. Deny deletes the file now. **Clean Up Now** runs the hourly expiry check at once.
- **Storage** is the studio's own folder, `data/studio3d/` (files are stored once by their SHA-256). New work is refused
  when less than 2 GB would be left free on the disk.
- **The 3D view** (`static/studio3d/viewport.js`, three.js 0.180.0 and its FBX loader, copied into `static/vendor/three/`):
  named views, perspective or orthographic lens, Frame, solid / wireframe / both, a colour per part, faulty faces marked in
  the danger colour, and a grid per axis sized to the model. Hide and Isolate in the parts list are only for looking and
  are not saved. The view never fetches pictures an FBX names by address.
- **Checks** look for bad numbers, bad indices, collapsed, repeated and zero-area faces, edges shared by more than two
  faces, and unused points. They do not judge whether a shape looks right.
- **Character Tools** needs the body mesh to have the role *Base anatomy*. It measures height, ground contact, centring,
  axes and units, asks you to state the pose and to confirm two things only a person can judge, and then records which
  exact file is handed to landmark mapping. It changes no geometry. Landmark mapping itself is not built.
- **Export** writes `Name.zip`: `Geometry/*.fbx` byte for byte, `AssetManifest.json` (classification, measurements, parts,
  SHA-256 checksums) and `Documentation/Compatibility_Report.md`. It is read back and checked before it is offered. If
  anything in it is pending you are asked first. Importing a SWIFT ZIP checks every path and checksum before storing
  anything and always makes a new asset; nothing already here is overwritten.
- **GLB files** are converted to FBX by Blender, run without a window in its own small container
  (`convert-service/`, started by `run.sh`, listening on this computer only at port 8193). The result is written in the
  studio's axes and centimetres, and the GLB is kept beside it as a *source file* that can be approved or denied on its
  own. GLB import stays off until **Test the converter** has passed on this computer: it converts a known one-metre cube
  and checks its size, direction and position. A different Blender version has to pass again. Only meshes, groups and
  armatures are carried over: no animation, cameras, lights or pictures.
- **Asset library.** **Add to Library** keeps an approved file for reuse in any project; it is a separate step from
  approving. **Use in this project** makes a new, already-approved asset from the same stored file (nothing is copied on
  disk) that can be changed without touching the library item. Items can be searched by name, tag and type, marked as
  favourites, grouped in flat collections, archived, and removed. Removing an item never changes assets made from it.
  Materials and texture sets are not in the library yet (`studio3d/library.py`).
- **Jobs.** Imports and exports go through one queue so nothing overlaps (`studio3d/jobs.py`). The same queue is where
  model-backed work will go; today every provider is part of the application.
- **Access.** Changes to `/api/studio3d/` are refused when they come from another website. Like the rest of the app
  there is no login: reach it on the PGX or over Tailscale only.

Code: `studio3d/` (`fbx.py` reader, `store.py`, `assets.py`, `jobs.py`, `package.py`, `character.py`, `api.py`) with its
tests in `studio3d/tests/`; the page is `static/studio3d/studio3d.js`.

## Reference Generation

A mode of the Asset Generator (it replaced the Character Generation placeholder) for making reference sheets of an
asset and cutting pictures out of them. The design is `docs/Reference_Generation_Design.md`; the 3D Generation design
it prepares for is in `3d_Generation_Plan/`. Code: `reference.py`, the `/api/reference` routes in `app.py`,
`static/generator/reference-generation.js`, tests in `tests/test_reference.py`.

**Built so far** (the design's section 16, steps 1 to 7, with the limits listed at the end):

- **Reference projects.** One per asset, several per SWIFT Director project, each with a name and a classification:
  four parents, eight asset types, optional subtype including Custom (the registry of `3d_Phase_3.md`).
- **Sheet Generation, Simple mode.** Up to four source pictures (Upload or Apply from Library), a prompt that follows
  the classification until you write your own, 1 to 10 complete sheets per run, a resolution (512, 1024, 2048, 4096 on
  the longer side) and a shape. The pixel size is shown. A size the image model cannot make is refused before anything
  is queued: today that is 4096, which needs an upscale model. The limit used for FLUX.2 (2048 pixels, about four
  megapixels) is its makers' figure and has not been measured on this machine.
- **Sheet Generation, Advanced mode.** The reference is built one row at a time. A row is one thing to document (the
  complete asset, base anatomy, a hat, a coat) in the Base or Detail section, with a name, a component role, and
  optionally its own classification. Each row has up to six source pictures, its own prompt, resolution, shape and
  candidate count (pictures per view slot), and six view slots: Front, Left, Right, Rear, Top, Bottom.
  - **Generate Row / Regenerate Row** makes candidates for every view that is switched on and not locked. **Generate
    Missing Views** fills only the empty ones. Each slot also has its own Generate / Regenerate and Upload. Before a row
    run, a box lists the views it will make, the ones left alone and why, and which pictures are sent.
  - New candidates never replace the picture in use: each slot has previous / next and **Use this one**. **Lock** keeps
    a view's picture through any run or upload; **Lock row** does the same for the whole row. A switched-off view is
    left out.
  - **Which pictures are sent.** The image model takes four. They are, in order: the row's sources, then the pictures
    of the row's locked views. A view that is not locked is never sent, because it may be the picture you are trying
    to replace. Anything beyond four is named as not sent.
  - **A locked view's picture cannot be denied** until the view is unlocked. The slot's other candidates still can.
    Locking does not stop the ten-day expiry: approve a picture to keep it.
  - **From another row** adds another row's view in use as a source. It is looked up each time, so nothing is copied,
    changing the other row regenerates nothing, and a view that has gone shows as not available. Rows cannot inherit
    from each other in a circle.
  - **Upload complete row** keeps one picture of the whole row for assembly. It is never split automatically.
  - An uploaded picture is kept (approved) because you supplied it. A picture placed from the Library that is still
    pending stays pending with the same expiry, and shares the file with the original.
  - Switching between Simple and Advanced loses nothing. The first switch to Advanced makes a first row from Simple's
    sources and selected sheet; nothing is extracted.
- **Specification panels.** One for the Base section and one for each Detail row: a Label and Sub Label (left empty,
  they are drawn as the name and role of what the panel belongs to), optional height, width and depth, and any number
  of named lines that can be added, reordered and removed. Each has its own "include on the sheet" tick; unticking it
  keeps everything typed. Dimensions are stored in centimetres and entered and shown in metres and centimetres or feet
  and inches (100 cm per metre, 30.48 per foot, 2.54 per inch); changing the units changes nothing stored.
- **Assembling the sheet.** `static/generator/reference-assembly.js` lays the pictures in use out on a canvas: Base
  rows on top, Detail rows below, in row order. No model is involved. Each picture keeps its proportions and is never
  enlarged past its own size; pictures of different sizes and shapes sit together without a fixed grid. A row can be
  full width, half, or a custom percent, and narrower rows sit side by side when they fit. Settings: orientation,
  width, automatic or fixed height (content taller than a fixed height is shrunk as a whole), solid or see-through
  background, alignment, spacing, margin, row and view names, a title, and boxed or plain panels. All text is drawn by
  the browser, so it is spelled as typed. **Preview** shows the result; **Save as a sheet** stores exactly that as a
  new pending sheet. Assembling changes no row or view and never starts an extraction.
- **An assembled sheet needs the pictures on it.** Approving one while some of those are still pending shows the
  design's "Approve Required Dependencies" question, listing each with the reason, and approves them together or not
  at all. A picture an approved sheet shows cannot be denied.
- **Consistency checks.** Each row lists plain facts the application can see by itself (views switched on but empty,
  views of different shapes, an inherited source with no picture). **Check consistency** has the language model compare
  the row's views in use and list where they disagree, each with the views involved, the kind of difference and
  whether it is sure (`assist.compare_references`, `POST /api/reference/check`). Findings are advice: they never stop
  a generation and change no picture. They are marked as out of date once the pictures in use change. A size the image
  model cannot make is different: that is refused before anything is queued.
- **Export.** Four choices, as in the design: Sheet Only; Individual Views; Complete Reference Package (sheet, views
  and `ReferenceManifest.json`); Component Reference Sheets (one sheet per row, assembled from that row, with its panel
  if ticked). An export contains only pictures that already exist: a sheet is never split into views to fill it, and
  nothing is generated. If anything in it is pending, the "Export Unapproved Asset" notice lists each picture and the
  time it has left; exporting changes no approval or expiry. The ZIP is made in the browser
  (`static/generator/zip-store.js`), and the server only says what belongs in it (`POST /api/reference/export`).
- **Hand-over to 3D Generation.** **Send to 3D Generation** records the selected sheet, each row's views in use and the
  extracted views, with the classification and each view's row and component role. Only ids are kept: no file is
  copied and nothing is approved. 3D Generation shows what was sent above its 3D view, with buttons back to Reference
  Generation, and has no picture-making controls of its own. A picture denied or expired later shows there as gone.
  Assigning these to 3D assets waits for the asset definition phase of `3d_Generation_Plan/`.
- **Sheet Extraction.** A separate workflow that only starts when you open it. Choose a sheet made here, upload one
  (PNG, JPEG, WebP) or take one from the Library; mark regions by dragging, or use **Detect regions**; say what each
  shows (a direction or a detail, Base or Detail, a component name) and adjust its edges by number; then extract. The
  browser cuts each region at the sheet's own pixels with no resizing, so the server needs no image library and
  nothing is invented. Detect regions finds separate shapes against a plain background and skips small or thin ones
  (usually text). It does not know what a shape is or which way it faces, so those are always yours to set.
- **Approval and retention.** Every sheet and extracted view starts as pending and is deleted ten days after it was
  made unless approved. Deny deletes it at once. Deleting removes the picture's record, prompt, settings and Library
  entry. Selecting, downloading or extracting from a pending picture does not approve it or move its expiry;
  downloading one asks first (the design's "Export Unapproved Asset" notice). Approval checks for other pending
  pictures the picture needs and asks before approving them together; nothing made so far has such needs, so that
  question first appears with Advanced assembly. A sweep runs hourly so pictures expire even if the page is closed.
- **Deleting the image file.** ComfyUI has no way to delete a file, so by default only SWIFT Director's records are
  deleted and the file stays in ComfyUI's input folder. Start with `COMFY_INPUT=/path/to/ComfyUI/input ./run.sh` and
  the file is deleted too, unless something else in the project still uses it.

**Not built yet, or limited:** choosing between providers (there is one, FLUX.2); specification panels on Simple
sheets; branding beyond the title line; background removal, padding and resizing of extracted views; and automatic
detection that suggests which way a view faces or tells text from pictures beyond a size rule. The design's step 8,
evaluating the image model against its constraints, needs the real model on the PGX and has not been done: every
check so far ran against stand-ins.

## Lore

Lore entries are stored on their own in `data/lore/<id>.json` and belong to no single project; a
storyboard lists the entries it uses. When Assisted Storyboard writes the story concept, recommends
the cast or plans the shots, the tagged entries are added to the request as background, with the
instruction to stay consistent with them and not to try to show all of it. Up to 12,000
characters per storyboard are sent, in the order tagged, so the most important text should come
first. Deleting an entry moves it to `data/trash/`.

In the Shots step every picture has an **In this picture** row of tick boxes, one per chosen actor and
per asset or prop. A ticked name has its reference picture sent with the description, and the image model
is told to keep it exactly; an unticked one is left out entirely. The language model's plan sets the boxes
at first, editing a description does not change them, and they are yours to set. With nothing ticked the
picture is made from the description and style alone, which is what an exterior shot with no cast wants.

In the story concept (step 3) every line of "What we see" can be edited, and locked. A locked line
cannot be edited, and **Write another** keeps locked lines word for word and writes the rest of
the story around them. Editing a line of an approved story also updates the approved copy, and
the segment's video prompt if that prompt was still the copied line.

## Identity Studio

A workspace for logos and app identities, opened from the sidebar. It is a separate module
(`identity_studio/` and `static/identity/`) with its own projects under `data/identity/`, and it
does not touch storyboard projects. Today it manages identity projects and their briefs; it does
not generate anything yet. Details: [identity_studio/README.md](identity_studio/README.md).

## SWIFT branding

The UI uses the SWIFT theme: `static/swift-tokens.css` (a vendored copy of the SWIFT tokens; do not edit the values, copy again from the source), the official wordmark and drop in `static/brand/` (unchanged), and Montserrat served locally from `static/fonts/` (SIL Open Font License). `static/index.html` defines no colours of its own: its variables map onto `--swift-*` tokens, and the "SWIFT skin" block at the end of its stylesheet applies them.

## Internal Design Notes:

Branding, themes, logos, shared components and UI standards for this app come from
[SWIFT_UI_and_Branding](https://github.com/Streamlined-Workflows/SWIFT_UI_and_Branding). Nothing about the SWIFT look is defined in this repository.
