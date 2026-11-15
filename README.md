# RunConvert SDK for Python

Convert files between 240+ formats (3,000+ conversions) with the [RunConvert API](https://www.runconvert.com/docs):
documents, images, video, audio, ebooks, archives, fonts and CAD. Python 3.10+, no dependencies.

```bash
pip install runconvert
```

```python
from runconvert import RunConvert

rc = RunConvert()  # reads RUNCONVERT_API_KEY

# One call: upload, convert, wait, download.
result = rc.convert("report.docx", "pdf", out_dir="out")
print(result.saved)  # ['out/report.pdf']
```

Get an API key in **Dashboard → API Keys** on [runconvert.com](https://www.runconvert.com). Keep keys on the server.

## Convert

```python
# From a URL (imported by RunConvert, nothing to upload)
rc.convert("https://example.com/talk.mp4", "mp3", options={"audioBitrate": "192k"})

# From bytes or a file object: give a filename (or from_)
rc.convert(data, "jpg", filename="photo.heic", out_dir="out")

# Progress and a time limit (seconds)
rc.convert("movie.mov", "mp4", out_dir="out", on_update=lambda job: print(job["status"]), timeout=600)
```

`convert` returns `ConvertResult(job, files, saved)`. Options for a conversion come from
`rc.operations.for_pair("mp4", "mp3")`.

## Jobs with your own tasks

```python
from runconvert import Jobs

# Merge three PDFs into one
job = rc.jobs.create({
    "a": {"operation": "import/url", "source_url": "https://example.com/cover.pdf"},
    "b": {"operation": "import/url", "source_url": "https://example.com/body.pdf"},
    "merge": {"operation": "convert", "input": ["a", "b"], "input_format": "pdf", "output_format": "pdf"},
    "export": {"operation": "export/url", "input": "merge"},
}, tag="merge")
done = rc.jobs.wait(job["id"])
for f in Jobs.exported_files(done):
    rc.files.download(f, "out")

# Upload your own file
job = rc.jobs.create({
    "upload": {"operation": "import/direct/upload"},
    "convert": {"operation": "convert", "input": "upload", "input_format": "png", "output_format": "webp", "image_quality": 80},
    "export": {"operation": "export/url", "input": "convert"},
})
upload_task = next(t for t in job["tasks"] if t["name"] == "upload")
rc.files.upload(upload_task, "image.png")
```

| Method | What it does |
|---|---|
| `rc.jobs.create(tasks, tag=, metadata=)` | Create a job (returned with its tasks) |
| `rc.jobs.get(id)` / `rc.jobs.list(status=, tag=, page=, per_page=)` | A job / one page `{"data", "meta"}` |
| `rc.jobs.all(...)` | Every job: `for job in rc.jobs.all()` |
| `rc.jobs.wait(id, timeout=, on_update=, raise_on_error=)` | Poll until `finished` or `error` |
| `rc.jobs.cancel(id)` / `rc.jobs.delete(id)` | Stop pending tasks / delete |
| `rc.files.upload(task, file)` / `rc.files.download(file, dir)` | Upload to a task / save a result |
| `rc.operations.list(category=)` / `for_input("mp4")` / `for_pair("mp4", "mp3")` | Formats, targets, engines (no key needed) |
| `rc.me()` | Account, team and credit balance |

## Webhooks

```python
from runconvert import construct_event

@app.post("/webhooks/runconvert")  # Flask
def runconvert_webhook():
    try:
        event = construct_event(request.get_data(), request.headers.get("X-RunConvert-Signature"), os.environ["RUNCONVERT_WEBHOOK_SECRET"])
    except ValueError:
        return "", 400
    if event["event"] == "job.finished":
        print("done", event["job"]["id"])
    return "", 200
```

## Errors

| Error | When |
|---|---|
| `RunConvertError` | The API said no: `.status`, `.code` (e.g. `BILLING_INSUFFICIENT_CREDITS`), `.message` |
| `JobFailedError` | The job ended in `error`; `.task` has `code`, `reason`, `message` |
| `WaitTimeoutError` | `wait` ran past its `timeout` |

Network errors, `429` and `5xx` are retried twice with backoff (`max_retries`). Creating a job is only
retried when the request never reached the API, so a job is never created twice.

## Development

```bash
PYTHONPATH=src python3 -m unittest tests.test_unit                          # no network
RUNCONVERT_API_KEY=rc_live_… PYTHONPATH=src python3 -m unittest tests.test_live  # real API, about 2 credits
```

## License

MIT
