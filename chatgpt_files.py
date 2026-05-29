"""
ChatGPT (OpenAI Apps SDK) file-attachment support.

The `create_scheduled_post` tool declares its `media` field as an OpenAI file
param (`_meta["openai/fileParams"] = ["media"]`). When the user attaches a file
in ChatGPT — or ChatGPT generates one — ChatGPT's runtime rewrites the file
reference into a file object `{download_url, file_id, mime_type, file_name}`
(or its download_url) BEFORE calling the server. Regular clients (Claude, etc.)
ignore `_meta` and keep sending plain public URL strings.

So `media` can arrive as a mix of:
  - plain URL strings (the normal flow, all clients)
  - file-descriptor objects (ChatGPT attachments) with a `download_url`

`coerce_media_items` flattens both into plain URL strings, which then flow
through the Feature 1 normalization like any other URL.

Pure stdlib so it can be unit-tested in isolation; accepts strings, dicts, and
objects with a `download_url` attribute.
"""


def _download_url_of(item) -> object:
    if isinstance(item, dict):
        return item.get("download_url")
    return getattr(item, "download_url", None)


def coerce_media_items(media) -> list[str]:
    """Normalize a mixed media list to plain URL strings.

    - str  -> kept as-is (skipped if blank)
    - dict / object with download_url -> its download_url (skipped if blank)
    - anything else -> ignored
    """
    urls: list[str] = []
    if not media:
        return urls
    for item in media:
        if isinstance(item, str):
            if item.strip():
                urls.append(item)
            continue
        url = _download_url_of(item)
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return urls
