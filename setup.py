import subprocess


def install_packages():
    subprocess.check_call(["pip", "install", "-r", "requirements.txt"])


if __name__ == "__main__":
    install_packages()
