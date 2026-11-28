#!/usr/bin/env bash
# Move the ComfyUI inside NVIDIA's "comfyui" container to a newer release, carefully.
#
#   ./comfy-update.sh check   [version]    look only: what would change. Nothing is installed or switched.
#   ./comfy-update.sh apply   [version]    snapshot, switch ComfyUI to that release, install what it needs, restart, re-apply the audio patch
#   ./comfy-update.sh rollback             put ComfyUI and its Python packages back as they were before the last apply
#
# version defaults to v0.34.0, the first release with the built-in TRELLIS.2 nodes.
#
# What it will not do: touch PyTorch. NVIDIA's container has its own build of torch for this machine; replacing it
# breaks the graphics card for everything. NVIDIA's torch packages and numpy are pinned at their installed versions,
# so pip stops with an error rather than swap them. Model files, inputs and outputs live outside the container and
# are not touched. apply refuses to start while ComfyUI is busy.
set -u
MODE="${1:-check}"; TARGET="${2:-v0.34.0}"; C=comfyui
NOTE=/opt/ComfyUI/user/swift-update
inside() { docker exec "$C" "$@"; }
say() { echo; echo "== $1"; }

docker inspect "$C" >/dev/null 2>&1 || { echo "PROBLEM: there is no container named $C."; exit 1; }

PINNED='^(torch|torchvision|torchaudio|torch[-_]tensorrt|torchao|torchdata|torchtitan|numpy)=='
pins() {      # NVIDIA's own builds exactly as installed, as pip constraints; and the release's needs without the torch lines
  inside sh -c "python -m pip list --format=freeze 2>/dev/null | grep -iE '$PINNED' > /tmp/swift-pins.txt; cat /tmp/swift-pins.txt"
  inside sh -c "git show '$TARGET:requirements.txt' | grep -viE '^(torch|torchvision|torchaudio)([^a-z]|\$)' > /tmp/swift-req.txt"
}

case "$MODE" in
check)
  say "Now"
  inside git log --oneline -1
  inside sh -c "python -m pip list 2>/dev/null | grep -iE '^(torch|torchvision|torchaudio|comfyui|comfy)'"
  say "Files in ComfyUI changed by hand (other than the placeholder files hidden by the mounted folders)"
  inside sh -c "git status --short | grep -v '^ D' || echo none"
  inside sh -c "git diff -- . ':(exclude)input' ':(exclude)models' | grep -E '^[+-][^+-]' | head -30"
  say "Fetching the list of releases (this only updates the list)"
  inside git fetch -q --tags origin || { echo "PROBLEM: could not reach GitHub from inside the container."; exit 1; }
  inside sh -c "git tag --sort=-v:refname | head -6"
  inside git rev-parse -q --verify "$TARGET^{commit}" >/dev/null || { echo "PROBLEM: there is no release called $TARGET."; exit 1; }
  say "Does $TARGET contain TRELLIS.2?"
  inside sh -c "git grep -il trellis '$TARGET' -- comfy_extras comfy | head -8 || echo 'NO - nothing named trellis in that release'"
  say "What $TARGET asks for that differs from now (requirements.txt)"
  inside sh -c "git diff HEAD '$TARGET' -- requirements.txt | grep -E '^[+-][^+-]' || echo 'no differences'"
  say "torch packages that will be pinned and left alone"
  pins
  say "What pip would install (a rehearsal; nothing is installed)"
  inside sh -c "python -m pip install --dry-run -c /tmp/swift-pins.txt -r /tmp/swift-req.txt 2>&1 | grep -E 'Would install|ERROR|conflict|Cannot|No matching' || echo 'nothing to install'"
  say "How the container was started (kept for the record)"
  docker inspect "$C" --format 'network={{.HostConfig.NetworkMode}} restart={{.HostConfig.RestartPolicy.Name}} ipc={{.HostConfig.IpcMode}} shm={{.HostConfig.ShmSize}} runtime={{.HostConfig.Runtime}} ports={{.HostConfig.PortBindings}} gpus={{.HostConfig.DeviceRequests}}'
  say "Is ComfyUI busy right now?"
  curl -s localhost:8188/queue | grep -o '"queue_running": *\[[^]]\{0,3\}' || echo "could not ask"
  echo; echo "Nothing was changed. Send Claude everything above."
  ;;
apply)
  if curl -s localhost:8188/queue | grep -q '"queue_running": *\[ *\['; then echo "ComfyUI is working on something. Wait for it to finish, then run this again. Nothing was changed."; exit 1; fi
  inside git rev-parse -q --verify "$TARGET^{commit}" >/dev/null || inside git fetch -q --tags origin
  inside git rev-parse -q --verify "$TARGET^{commit}" >/dev/null || { echo "PROBLEM: there is no release called $TARGET. Nothing was changed."; exit 1; }
  say "Taking a snapshot of ComfyUI as it is now (image comfyui-backup:before-update)"
  docker commit "$C" comfyui-backup:before-update >/dev/null && echo "Snapshot taken." || { echo "PROBLEM: the snapshot failed. Nothing was changed."; exit 1; }
  inside sh -c "git rev-parse HEAD > $NOTE-prev.txt && python -m pip list --format=freeze > $NOTE-pip-before.txt && git diff -- requirements.txt > $NOTE-requirements.patch"
  BEFORE=$(inside python -c 'import torch; print(torch.__version__)')
  pins >/dev/null
  say "Installing what $TARGET needs (torch is pinned and left alone)"
  if ! inside python -m pip install -q -c /tmp/swift-pins.txt -r /tmp/swift-req.txt; then echo "PROBLEM: pip stopped. ComfyUI itself was not switched. Run: ./comfy-update.sh rollback   and send Claude the lines above."; exit 1; fi
  AFTER=$(inside python -c 'import torch; print(torch.__version__)')
  if [ "$BEFORE" != "$AFTER" ]; then echo "PROBLEM: torch changed from $BEFORE to $AFTER. Run: ./comfy-update.sh rollback   and send Claude the lines above."; exit 1; fi
  say "Switching ComfyUI to $TARGET"
  inside git checkout -q -- requirements.txt      # NVIDIA's hand edit to this list is saved in $NOTE-requirements.patch and put back by rollback
  inside git checkout -q "$TARGET" || { echo "PROBLEM: ComfyUI could not be switched. Run: ./comfy-update.sh rollback   and send Claude the lines above."; exit 1; }
  inside sed -i -E '/^(torch|torchvision|torchaudio)$/d' requirements.txt      # the same edit NVIDIA made, so nothing later reinstalls torch from this list
  inside git log --oneline -1
  say "Restarting ComfyUI with the audio patch re-applied"
  ./patch-comfy-audio.sh || docker restart "$C" >/dev/null
  for i in $(seq 1 60); do curl -s localhost:8188/system_stats >/dev/null 2>&1 && break; sleep 5; done
  say "Result"
  curl -s localhost:8188/system_stats | grep -o 'comfyui_version[^,]*' || echo "PROBLEM: ComfyUI did not come back within 5 minutes. Run: docker logs --tail 40 comfyui   and send Claude the lines."
  curl -s localhost:8188/object_info | grep -q 'VaeDecodeStructureTrellis2' && echo "TRELLIS.2 nodes: present" || echo "TRELLIS.2 nodes: NOT found"
  echo "torch: $AFTER (unchanged)"
  echo "Add-ons that failed to load, if any:"; docker logs --since 10m "$C" 2>&1 | grep -iA12 'import failed' | grep -iE 'custom_nodes|IMPORT FAILED' | sort -u | head -12 || true
  echo; echo "Now make one image and one short video in SWIFT Director to confirm they still work. To undo: ./comfy-update.sh rollback"
  ;;
rollback)
  inside test -f "$NOTE-prev.txt" || { echo "There is no record of an update to undo."; exit 1; }
  say "Putting ComfyUI back"
  inside sh -c "git checkout -q -- requirements.txt; git checkout -q \$(cat $NOTE-prev.txt) && git log --oneline -1; [ -s $NOTE-requirements.patch ] && git apply $NOTE-requirements.patch 2>/dev/null; true"
  say "Putting back the Python packages that changed"
  inside sh -c "python -m pip list --format=freeze > /tmp/swift-now.txt; grep -vxFf /tmp/swift-now.txt $NOTE-pip-before.txt | grep -viE '$PINNED' | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/swift-back.txt; cat /tmp/swift-back.txt; [ -s /tmp/swift-back.txt ] && python -m pip install -q --no-deps -r /tmp/swift-back.txt || echo 'none needed'"
  ./patch-comfy-audio.sh || docker restart "$C" >/dev/null
  for i in $(seq 1 60); do curl -s localhost:8188/system_stats >/dev/null 2>&1 && break; sleep 5; done
  curl -s localhost:8188/system_stats | grep -o 'comfyui_version[^,]*' || echo "PROBLEM: ComfyUI did not come back. The full snapshot is the image comfyui-backup:before-update; send Claude the output of: docker logs --tail 40 comfyui"
  ;;
*) echo "Use: ./comfy-update.sh check | apply | rollback   [version]"; exit 1 ;;
esac
