"""
Caption drafts in @thirddimension.pk's own style (core/brand_style.json):
a short hook with an emoji, the product name, the site, then hashtags after
".\n." separators. Deterministic, no LLM; the Manager can refine the draft.
Drafts are only ever returned for review, never posted.

Hooks reuse the account's own best lines per theme. A `question` is added on
request: the account's widest-reaching Reels asked followers something.
"""
import json
import os

BRAND = json.load(open(os.path.join(os.path.dirname(__file__), "brand_style.json"), encoding="utf-8"))

# theme keyword -> (hook, hashtags). Hooks are lines that already ran on the account.
THEMES = [
    (("batman", "gotham", "joker", "dark knight"), "Gotham called. Your desk answered. \U0001F987",
     ["#batman", "#dccomics", "#batmancollection"]),
    (("f1", "formula", "racing", "brake"), "Your desk. Your paddock. \U0001F3CE️",
     ["#f1", "#formula1", "#f1fan", "#racingfan", "#formula1pakistan"]),
    (("dragon ball", "dbz", "goku", "vegeta"), "Light up your DBZ world. \U0001F525",
     ["#dragonball", "#dbz", "#anime", "#animepakistan", "#otaku"]),
    (("lord of the rings", "lotr", "ring", "hobbit", "gandalf"), "One ring to shine on all. \U0001F48D",
     ["#lordoftherings", "#lotr", "#tolkien"]),
    (("jurassic", "dino", "t-rex", "trex"), "Jurassic Park had scientists. We have a printer. \U0001F995",
     ["#jurassicpark", "#dinosaur"]),
    (("cat", "kitten"), "For the cat people. \U0001F408",
     ["#catlovers", "#catsofinstagram"]),
    (("lamp", "light", "lithophane"), "Light it up. \U0001F4A1",
     ["#desklamp", "#roomdecor"]),
    (("keychain", "key chain"), "Your keys finally have a personality. \U0001F511",
     ["#keychain", "#accessories"]),
]
DEFAULT_HOOK = "We don't just print plastic. We print the things you care about. \U0001F49C"


def _theme(text):
    t = (text or "").lower()
    for keys, hook, tags in THEMES:
        if any(k in t for k in keys):
            return hook, tags
    return DEFAULT_HOOK, []


def draft_caption(product_title=None, price=None, hook=None, question=None, extra_hashtags=None, hashtags=True):
    auto_hook, theme_tags = _theme(product_title)
    lines = [hook or auto_hook]
    if question:
        lines.append(question.strip())
    if product_title:
        lines.append(f"{product_title}" + (f" , Rs {float(price):,.0f}" if price else ""))
    lines.append(BRAND["captions"]["cta"])
    caption = "\n\n".join(lines)
    tags = []
    if hashtags:
        for t in BRAND["captions"]["hashtags_core"] + theme_tags + list(extra_hashtags or []):
            t = t if t.startswith("#") else f"#{t}"
            if t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
        tags = tags[:BRAND["captions"]["hashtags_max"]]
        caption += "\n" + BRAND["captions"]["hashtags_separator"] + "\n" + " ".join(tags)
    return {"caption": caption, "hashtags": tags,
            "note": "Draft only. Nothing is posted until a publish preview is approved."}
