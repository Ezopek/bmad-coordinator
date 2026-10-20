def ext4_mountinfo(root, fstype="ext4"):
    return f"36 25 0:32 / {root} rw,relatime - {fstype} /dev/test rw\n"
