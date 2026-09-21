import os, sys
root = sys.argv[1]
bad = []
for dp, _, fs in os.walk(root):
    for n in fs:
        p = os.path.join(dp, n)
        try:
            data = open(p, "rb").read()
        except OSError:
            continue
        if b"\r" in data and (dp.endswith("/cmd") or n.endswith(".sh") or n in ("main", "install_callback", "setup.sh")):
            bad.append(p)
print("CR leftovers:", bad if bad else "NONE")
sample = os.path.join(root, "cmd", "install_callback")
if os.path.isfile(sample):
    print("install_callback head:", open(sample, "rb").read(60))
