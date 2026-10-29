import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name=='nt','Linux installer')
class DockerInstallTests(unittest.TestCase):
    def setUp(self):
        self.script=Path(__file__).resolve().parents[1]/'scripts/Install-Docker.sh'

    def test_syntax(self):
        subprocess.run(['bash','-n',str(self.script)],check=True)

    def test_supported_os_detection_does_not_install_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            release=Path(directory)/'os-release'
            for distro,suite in [('ubuntu','noble'),('debian','bookworm')]:
                release.write_text(f'ID={distro}\nVERSION_CODENAME={suite}\n')
                result=subprocess.run(['bash','-c','source "$1"; detect_docker_os "$2"; printf "%s:%s" "$DOCKER_DISTRO" "$DOCKER_SUITE"','test',str(self.script),str(release)],capture_output=True,text=True,check=True)
                self.assertEqual(result.stdout,f'{distro}:{suite}')

    def test_unsupported_os_stops_before_privileged_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            release=Path(directory)/'os-release'; release.write_text('ID=other\nVERSION_CODENAME=unknown\n')
            result=subprocess.run(['bash','-c','source "$1"; detect_docker_os "$2"','test',str(self.script),str(release)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)

    def test_conflicting_runtime_is_not_removed(self):
        result=subprocess.run(['bash','-c','source "$1"; dpkg-query() { printf installed; }; check_docker_conflicts','test',str(self.script)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('docker.io',result.stderr)

    def test_launcher_installs_missing_docker_without_confirmation(self):
        script=self.script.parents[1]/'start.sh'
        text=script.read_text()
        self.assertIn('bash scripts/Install-Docker.sh',text)
        self.assertNotIn('--native',text)
        installer=self.script.read_text()
        self.assertNotIn('apt-get remove',installer)
        self.assertNotIn('get.docker.com',installer)
