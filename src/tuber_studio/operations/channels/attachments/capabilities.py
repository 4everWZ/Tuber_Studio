"""Attachment capability for a Bilibili comment destination."""


def video_top_level_images(resource, root, parent):
    return resource in ("video", 1) and str(root) == str(parent) == "0"
