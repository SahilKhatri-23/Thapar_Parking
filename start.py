"""
One-command start:  python start.py
 - applies migrations, seeds demo data on first run, downloads vision models
 - prints the LAN address + QR code for the mobile app
 - runs Django on 0.0.0.0:8000 so phones on the same Wi-Fi can connect
"""
import os
import socket
import subprocess
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
PORT = os.environ.get("PORT", "8000")


def run(*args):
    return subprocess.call([sys.executable, "manage.py", *args])


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    first = not os.path.exists(os.path.join(HERE, "db.sqlite3"))
    run("migrate", "--noinput", "-v", "0")
    if first:
        run("seed")
    if "--skip-models" not in sys.argv:
        run("warmup")
    url = f"http://{lan_ip()}:{PORT}"
    print("\n" + "=" * 64)
    print("  Thapar Smart Parking backend")
    print(f"  Web portal (this PC):   http://localhost:{PORT}/portal/")
    print(f"  Mobile app server URL:  {url}")
    print("  Demo logins: admin@thapar.edu / guard@thapar.edu / faculty@thapar.edu /")
    print("               student@thapar.edu / vendor@example.com / visitor@example.com")
    print("  Password:    Thapar@123")
    print("=" * 64)
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)
        print("  ↑ scan this from the app: Server settings → Scan QR\n")
    except Exception:
        pass
    run("runserver", f"0.0.0.0:{PORT}")
