import os, subprocess, time


def pids():
    out = subprocess.check_output(["ps", "-C", "chromium", "-o", "pid="], text=True)
    return [int(x) for x in out.split()]


def cpu():
    total = 0
    for pid in pids():
        try:
            parts = open(f"/proc/{pid}/stat").read().split()
            total += int(parts[13]) + int(parts[14])
        except OSError:
            pass
    return total


def sample(sec):
    a, t = cpu(), time.time()
    time.sleep(sec)
    b, d = cpu(), time.time() - t
    print(f"{d:4.1f}s chromium {100 * (b - a) / os.sysconf('SC_CLK_TCK') / d:5.1f}% of one core", flush=True)


if __name__ == "__main__":
    for _ in range(12):
        sample(3)
