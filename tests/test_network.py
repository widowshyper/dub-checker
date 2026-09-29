import os
import unittest

from dubchecker.scanner import _mount_for, is_network_path, parse_mount_output, parse_proc_mounts

PROC_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
//nas/media /mnt/nas cifs rw,vers=3.0 0 0
nas:/export/anime /mnt/nfs\\040anime nfs4 rw 0 0
tmpfs /run tmpfs rw 0 0
"""

MAC_MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
//me@nas._smb._tcp.local/Media on /Volumes/Media (smbfs, nodev, nosuid, mounted by me)
/dev/disk5s1 on /Volumes/USB (exfat, local, nodev, nosuid, noowners)
"""


class NetworkPathTests(unittest.TestCase):
    def test_unc_paths(self) -> None:
        self.assertTrue(is_network_path("\\\\server\\share\\Anime"))
        self.assertTrue(is_network_path("//server/share/Anime"))
        self.assertFalse(is_network_path(""))

    def test_linux_mounts(self) -> None:
        mounts = parse_proc_mounts(PROC_MOUNTS)
        self.assertEqual(_mount_for("/mnt/nas/anime/Show", mounts), "cifs")
        self.assertEqual(_mount_for("/mnt/nfs anime/Show", mounts), "nfs4")
        self.assertEqual(_mount_for("/home/me/anime", mounts), "ext4")
        self.assertEqual(_mount_for("/mnt/nasty", mounts), "ext4")  # a prefix of the name isn't a match

    def test_mac_mounts(self) -> None:
        mounts = parse_mount_output(MAC_MOUNT)
        self.assertEqual(_mount_for("/Volumes/Media/Anime", mounts), "smbfs")
        self.assertEqual(_mount_for("/Volumes/USB/Anime", mounts), "exfat")

    @unittest.skipUnless(os.name == "nt", "Windows drive types")
    def test_windows_local_and_admin_share(self) -> None:
        self.assertFalse(is_network_path(os.environ.get("SystemDrive", "C:") + "\\"))
        self.assertTrue(is_network_path("\\\\localhost\\C$\\Windows"))


if __name__ == "__main__":
    unittest.main()
