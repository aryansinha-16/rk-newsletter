# -*- coding: utf-8 -*-
"""Email-safe HTML renderer for the newsletter ("Broadsheet" design).

The model no longer writes HTML. It returns structured content via the
submit_edition tool and this module turns that into the email, deterministically.
Two things follow from that split:

  * The layout is identical every day. It used to be improvised per run from a
    single prompt line ("clean white background, Arial font"), so no two
    editions matched and nothing enforced email-client safety.
  * The "no major news today (last major news: ...)" line is now produced by
    code, not by the model, so it can't be silently dropped for a company.

Email constraints honoured here:
  * <table> layout only. No flex, no grid, no float — Outlook renders tables.
  * Every visual rule is an INLINE style attribute. The <style> block carries
    only @media overrides, which are a bonus, never load-bearing.
  * Explicit background AND colour on every text container, so a recipient's
    dark mode cannot leave dark text on a dark ground.
  * 600px shell, >=13px body text, ~15KB output (Gmail clips at 102KB).

Edition dict:
    brand, tagline, date_display, exec_summary[str], footer,
    sections[{label, items[{company, fresh, bullets[{headline, why, url}],
                            last, last_date}]}]
"""

import html as _html

WIDTH = 600
INK, MUTED, RULE, ACCENT, FAINT = "#191714", "#6b635a", "#d9d2c5", "#8a2f2a", "#a89f92"
SERIF = "Georgia,'Times New Roman',Times,serif"
CANVAS = "#f2efe9"

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def esc(s):
    return _html.escape(str(s or ""), quote=True)


def short_date(iso):
    """'2026-07-30' -> '30 Jul'. Shows how stale a last-known item is."""
    if not iso:
        return ""
    try:
        _y, m, d = (int(x) for x in str(iso).split("-"))
        return f"{d} {_MON[m - 1]}"
    except Exception:
        return ""


def _masthead(ed):
    return f"""      <tr><td class="pad" align="center"
          style="background:#ffffff;padding:34px 40px 18px 40px;border-top:3px solid {INK};">
        <div style="font:400 10px/1 {SERIF};letter-spacing:.34em;text-transform:uppercase;color:{ACCENT};">
          {esc(ed['tagline'])}</div>
        <div class="h1" style="font:400 33px/1.12 {SERIF};color:{INK};padding:12px 0 10px 0;
                    letter-spacing:-.01em;">{esc(ed['brand'])}</div>
        <div style="border-top:1px solid {RULE};border-bottom:3px double {RULE};padding:8px 0;
                    font:400 11px/1 {SERIF};letter-spacing:.16em;text-transform:uppercase;color:{MUTED};">
          {esc(ed['date_display'])}</div>
      </td></tr>"""


def _brief(ed):
    if not ed.get("exec_summary"):
        return ""
    items = "".join(f"""<tr>
                  <td width="34" valign="top"
                      style="padding:9px 0;font:400 15px/1.4 {SERIF};color:{ACCENT};">{i + 1:02d}</td>
                  <td valign="top" style="padding:9px 0;font:400 15px/1.55 {SERIF};color:{INK};
                             border-bottom:1px solid {RULE};">{esc(t)}</td>
                </tr>""" for i, t in enumerate(ed["exec_summary"]))
    return f"""      <tr><td class="pad" style="background:#ffffff;padding:20px 40px 8px 40px;">
        <div style="font:400 10px/1 {SERIF};letter-spacing:.28em;text-transform:uppercase;color:{MUTED};">
          The Brief</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="margin-top:4px;">{items}</table>
      </td></tr>"""


def _section_label(label):
    return f"""      <tr><td class="pad" style="background:#ffffff;padding:26px 40px 0 40px;">
        <div style="font:400 11px/1 {SERIF};letter-spacing:.24em;text-transform:uppercase;color:{ACCENT};
                    border-bottom:1px solid {INK};padding-bottom:7px;">{esc(label)}</div>
      </td></tr>"""


def _company(item):
    bullets = "".join(f"""
          <div style="padding:0 0 14px 0;">
            <a href="{esc(x['url'])}"
               style="font:400 19px/1.32 {SERIF};color:{INK};">{esc(x['headline'])}</a>
            <div style="font:400 14px/1.62 {SERIF};color:{MUTED};padding-top:6px;">{esc(x['why'])}</div>
            <a href="{esc(x['url'])}"
               style="font:400 11px/1 {SERIF};letter-spacing:.14em;text-transform:uppercase;
                      color:{ACCENT};display:inline-block;padding-top:8px;">Read more &rarr;</a>
          </div>""" for x in item["bullets"])
    return f"""      <tr><td class="pad" style="background:#ffffff;padding:18px 40px 4px 40px;">
        <div style="font:700 12px/1 {SERIF};letter-spacing:.12em;text-transform:uppercase;color:{INK};
                    padding-bottom:10px;">{esc(item['company'])}</div>{bullets}
      </td></tr>"""


def _quiet(quiet):
    lines = "".join(f"""<tr>
                  <td width="146" valign="top" style="width:146px;padding:6px 12px 6px 0;
                             font:700 11px/1.4 {SERIF};letter-spacing:.06em;text-transform:uppercase;
                             color:{MUTED};">{esc(q['company'])}</td>
                  <td valign="top" style="padding:6px 0;font:italic 400 13px/1.5 {SERIF};color:{MUTED};">
                    {esc(q.get('last') or 'Nothing on record.')}{
        f'<span style="font-style:normal;font-size:10px;letter-spacing:.1em;color:{FAINT};">'
        f'&nbsp;{short_date(q.get("last_date"))}</span>' if q.get("last_date") else ''}</td>
                </tr>""" for q in quiet)
    return f"""      <tr><td class="pad" style="background:#ffffff;padding:8px 40px 4px 40px;">
        <div style="border-top:1px solid {RULE};padding-top:10px;font:400 10px/1 {SERIF};
                    letter-spacing:.22em;text-transform:uppercase;color:{MUTED};">
          Quiet today &mdash; last known</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="margin-top:4px;table-layout:fixed;">{lines}</table>
      </td></tr>"""


def _footer(ed):
    return f"""      <tr><td class="pad" align="center"
          style="background:#ffffff;padding:26px 40px 34px 40px;border-bottom:3px solid {INK};">
        <div style="border-top:1px solid {RULE};padding-top:16px;font:400 10px/1.7 {SERIF};
                    letter-spacing:.16em;text-transform:uppercase;color:{MUTED};">{esc(ed['footer'])}</div>
      </td></tr>"""


def render(ed):
    """Edition dict -> full HTML email body."""
    rows = [_masthead(ed), _brief(ed)]
    for sec in ed["sections"]:
        if sec.get("label"):
            rows.append(_section_label(sec["label"]))
        quiet = [i for i in sec["items"] if not i.get("fresh")]
        for item in sec["items"]:
            if item.get("fresh"):
                rows.append(_company(item))
        if quiet:
            rows.append(_quiet(quiet))
    rows.append(_footer(ed))
    inner = "\n".join(r for r in rows if r)

    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  a {{ text-decoration: none; }}
  @media only screen and (max-width: 620px) {{
    .shell {{ width: 100% !important; }}
    .pad {{ padding-left: 18px !important; padding-right: 18px !important; }}
    .h1 {{ font-size: 24px !important; }}
  }}
</style>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin:0;padding:0;background:{CANVAS};">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" class="shell" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0"
           style="width:{WIDTH}px;max-width:100%;">
{inner}
    </table>
  </td></tr>
</table>"""
