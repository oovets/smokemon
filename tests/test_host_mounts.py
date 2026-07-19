"""_mounts_linux: which /proc/mounts entries become disk.used_pct / disk.inode_used_pct
signals. squashfs loop mounts must never reach the detector -- see host.py for why."""

from unittest.mock import mock_open, patch

from smokemon.probes import host

_PROC_MOUNTS = (
    "/dev/nvme0n1p1 / ext4 rw,relatime 0 0\n"
    "/dev/loop0 /snap/core22/2140 squashfs ro,nodev,relatime 0 0\n"
    "/dev/loop1 /snap/go/10988 squashfs ro,nodev,relatime 0 0\n"
    "/dev/loop2 /snap/snapd/25585 squashfs ro,nodev,relatime 0 0\n"
    "/dev/nvme0n1p2 /boot/efi vfat rw,relatime 0 0\n"
    "tmpfs /run tmpfs rw,relatime 0 0\n"          # not /dev/*, already excluded either way
    "overlay /var/lib/docker/overlay2/abc merged overlay rw,relatime 0 0\n"
)


def test_squashfs_loop_mounts_are_excluded():
    """Every snap revision is its own read-only compressed image that reads ~100% used and
    ~100% inode-used by construction -- not a capacity signal. A box with 40 snap revisions
    must not open 80 permanent incidents for it."""
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS)):
        mounts = host._mounts_linux()
    assert "/snap/core22/2140" not in mounts
    assert "/snap/go/10988" not in mounts
    assert "/snap/snapd/25585" not in mounts


def test_real_device_mounts_are_kept():
    with patch("builtins.open", mock_open(read_data=_PROC_MOUNTS)):
        mounts = host._mounts_linux()
    assert "/" in mounts
    assert "/boot/efi" in mounts


def test_falls_back_to_root_when_proc_mounts_unreadable():
    with patch("builtins.open", side_effect=OSError):
        assert host._mounts_linux() == ["/"]


def test_falls_back_to_root_when_nothing_survives_filtering():
    only_squashfs = "/dev/loop0 /snap/x/1 squashfs ro 0 0\n"
    with patch("builtins.open", mock_open(read_data=only_squashfs)):
        assert host._mounts_linux() == ["/"]
