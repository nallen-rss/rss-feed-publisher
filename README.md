# Static RSS Feed Publisher

Static RSS generation from FreshRSS OPML templates.

GitHub Actions reads `templates/*.opml`, scrapes each `HTML+XPath` outline, writes RSS XML into `public/feeds/`, and publishes the result with GitHub Pages.

## Repository Layout

```text
.github/workflows/publish-feed.yml
requirements.txt
scripts/build_feed.py
templates/
```

## Add Feed Templates

1. Configure an `HTML+XPath` feed in FreshRSS.
2. Export OPML from FreshRSS.
3. Place the OPML file in `templates/`.
4. Commit the OPML file.
5. Run the GitHub Actions workflow.

Every `HTML+XPath` outline in `templates/*.opml` becomes one RSS XML file.

## Published URLs

After GitHub Pages deployment:

```text
https://GITHUB_USERNAME.github.io/REPOSITORY_NAME/
https://GITHUB_USERNAME.github.io/REPOSITORY_NAME/feeds/FEED_SLUG.xml
```

`public/feed.xml` is also written as a compatibility alias for the first template.

## Required FreshRSS OPML Fields

Each template needs these FreshRSS OPML attributes:

```xml
frss:xPathItem
frss:xPathItemTitle
frss:xPathItemUri
```

Optional supported fields:

```xml
frss:xPathItemContent
frss:xPathItemAuthor
frss:xPathItemTimestamp
frss:xPathItemThumbnail
frss:xPathItemCategories
frss:xPathItemUid
frss:xPathItemTimeFormat
```

## GitHub Setup

1. Create a public GitHub repository.
2. Upload this project.
3. Add FreshRSS OPML files to `templates/`.
4. Open repository Settings.
5. Open Pages.
6. Set Build and deployment Source to GitHub Actions.
7. Open Actions.
8. Run `Publish RSS feeds` manually once.
9. Add each published `feeds/*.xml` URL to Inoreader.

## Local Test

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/build_feed.py
python -m http.server 8000 --directory public
```

Then open:

```text
http://localhost:8000/
```
