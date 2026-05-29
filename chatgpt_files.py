"""
ChatGPT (OpenAI Apps SDK) file-picker support.

When ChatGPT attaches files via its file picker — or generates images — it
hands the tool a list of file descriptors, each with a ``download_url``. The
``create_scheduled_post`` tool exposes a ``mediaFiles`` parameter (flagged in
the tool's ``_meta`` via ``openai/fileParams``) so ChatGPT renders the picker;
this module turns those descriptors into plain media URLs that get appended to
the post's media list before scheduling — so they flow through the Feature 1
normalization just like any other URL.

Mirrors the Java SchedulerOpenAiFileToolRewriter merge behaviour, adapted to
this project (where media is a list of URL strings and the tool takes flat
parameters instead of an ``info`` JSON string).

Pure stdlib so it can be unit-tested in isolation; accepts both dicts and
objects with a ``download_url`` attribute.
"""


def _download_url_of(file) -> object:
    if isinstance(file, dict):
        return file.get("download_url")
    return getattr(file, "download_url", None)


def extract_download_urls(media_files) -> list[str]:
    """Pull the valid download_url out of each ChatGPT file descriptor.

    Entries that are not objects/dicts, or whose download_url is missing,
    non-string, or blank, are ignored.
    """
    urls: list[str] = []
    if not media_files:
        return urls
    for file in media_files:
        url = _download_url_of(file)
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return urls
