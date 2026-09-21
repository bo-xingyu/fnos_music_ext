#!/usr/bin/env bash
set -euo pipefail

PYWIN='D:\Program Files\Xiaomi MiMo\resources\runtimes\win32-x64\python\python.exe'
PYMSYS='/d/Program Files/Xiaomi MiMo/resources/runtimes/win32-x64/python/python.exe'
SHIM='/tmp/pyshim-fnmusic'
mkdir -p "${SHIM}"

if [ -f "${PYMSYS}" ]; then
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "${PYMSYS}" > "${SHIM}/python3"
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "${PYMSYS}" > "${SHIM}/python"
  chmod +x "${SHIM}/python3" "${SHIM}/python"
elif [ -f "${PYWIN}" ]; then
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "${PYWIN}" > "${SHIM}/python3"
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "${PYWIN}" > "${SHIM}/python"
  chmod +x "${SHIM}/python3" "${SHIM}/python"
else
  echo "ERROR: bundled python not found" >&2
  ls -la "/d/Program Files/Xiaomi MiMo/resources/runtimes/win32-x64/python/" 2>&1 || true
  exit 1
fi

# Strip WindowsApps stub from PATH; put shim first
CLEAN_PATH="${SHIM}:/c/Git/bin:/c/Git/cmd:/usr/bin:/bin"
export PATH="${CLEAN_PATH}"

cd /c/Users/PC/XiaomiMiMoProjects/fnos_music_ext
# 工作区里的 shell 脚本也先洗成 LF，避免 cp -a 把 CRLF 带进 STAGE
python3 - <<'PY'
import os
root = r"C:/Users/PC/XiaomiMiMoProjects/fnos_music_ext"
targets = []
for base in ("fpk/cmd", "fpk/payload/bin"):
    d = os.path.join(root, base)
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                targets.append(p)
for p in targets:
    data = open(p, "rb").read()
    new = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if new != data:
        open(p, "wb").write(new)
        print("LF", os.path.relpath(p, root))
print("workspace scripts normalized")
PY
echo "python3=$(command -v python3)"
python3 -c "import sys; from PIL import Image; print(sys.executable, 'Pillow OK')"
VER="$(head -n 1 VERSION | tr -d '[:space:]')"
bash ./build_fpk.sh --version "${VER}"
