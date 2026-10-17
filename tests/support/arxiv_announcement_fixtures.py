"""Documented RSS 2.0 announcement metadata, without network access."""

from html import escape


BATCH = "Fri, 16 Oct 2026 00:00:00 -0400"


def rss_item(
    identifier="2510.00001v1",
    *,
    stamp=BATCH,
    announce_type="new",
    title="Portfolio risk model",
    abstract="We estimate financial market risk from returns.",
):
    return f"""<item>
      <title>{escape(title)}</title>
      <link>https://arxiv.org/abs/{identifier.removesuffix("v1")}</link>
      <description>{escape(f"arXiv:{identifier} Announce Type: {announce_type} Abstract: {abstract}")}</description>
      <guid isPermaLink="false">oai:arXiv.org:{identifier}</guid>
      <category>q-fin.RM</category><category>cs.LG</category>
      <pubDate>{escape(stamp)}</pubDate>
      <arxiv:announce_type>{escape(announce_type)}</arxiv:announce_type>
      <dc:creator>Alice Author, Bob Writer</dc:creator>
    </item>"""


def rss_feed(items="", *, batch=BATCH, built="Fri, 16 Oct 2026 04:00:11 +0000"):
    return f"""<?xml version="1.0"?>
    <rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel><title>arXiv subject announcements</title>
      <link>https://rss.arxiv.org/rss/q-fin.RM</link>
      <description>arXiv announcements</description>
      <pubDate>{escape(batch)}</pubDate><lastBuildDate>{escape(built)}</lastBuildDate>
      {items}</channel></rss>"""
