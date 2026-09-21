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
echo "python3=$(command -v python3)"
python3 -c "import sys; from PIL import Image; print(sys.executable, 'Pillow OK')"
command -v tar
command -v gzip
command -v md5sum
bash ./build_fpk.sh --version 2.10.0
