// the hero, the checklist and the Try-it examples: written by demo/build_site.py -- do not edit by hand
window.MAGELLAN_SHOWCASE = {
 "version": "0.2.0",
 "hero": {
  "command": "magellan-lite check",
  "verdict": "block",
  "output": "magellan-lite: BLOCK \u00b7 1 finding \u00b7 7 changes in 3 files (against git:HEAD)\n\n  changes\n     del  sensor.api.handle_legacy_upload  sensor/api.py:16\n     del  sensor.api.weigh_one             sensor/api.py:21\n     del  sensor.pipeline.process_legacy   sensor/pipeline.py:40\n     sig  sensor.channel.parse_record      sensor/channel.py:7\n    body  sensor.pipeline.process          sensor/pipeline.py:27\n     new  sensor.channel.LAYOUT_V3         sensor/channel.py:3\n     new  sensor.channel.LAYOUT_V4         sensor/channel.py:4\n\n  reaches 6 definitions the change did not touch (score fades with distance)\n    0.85  1 hop   sensor.collector.collect       sensor/collector.py:7\n          because sensor.collector.collect calls sensor.channel.parse_record (sensor/collector.py:11)\n    0.77  2 hops  sensor.collector.sweep         sensor/collector.py:17\n          because sensor.collector.sweep calls sensor.collector.collect (sensor/collector.py:18)\n    0.69  3 hops  sensor.main.run_agent          sensor/main.py:8\n          because sensor.main.run_agent calls sensor.collector.sweep (sensor/main.py:10)\n    0.54  1 hop   sensor.api.handle_fast_upload  sensor/api.py:12\n    0.54  1 hop   sensor.api.handle_upload       sensor/api.py:8\n    0.54  1 hop   sensor.pipeline.process_fast   sensor/pipeline.py:36\n\n  checklist\n    [ ] CRITICAL signature-break  sensor/collector.py:11\n        sensor.collector.collect calls parse_record() the old way: it now requires layout, which the call does not pass\n        sensor.channel.parse_record went from (fields: list[str]) -> dict[str, str] to (fields: list[str], layout: str) -> dict[str, str]. It is in code this change did not touch, so nobody updated it: the call raises TypeError when it runs.\n        fix: Update the call, or give the new parameter a default so existing calls keep working."
 },
 "rules": [
  {
   "id": "assert-on-tuple",
   "severity": "medium",
   "blocking": false,
   "kind": "file",
   "fix": "Drop the parentheses: `assert x, \"message\"`."
  },
  {
   "id": "bare-except",
   "severity": "low",
   "blocking": false,
   "kind": "file",
   "fix": "Catch what you expect, e.g. `except ValueError:`."
  },
  {
   "id": "compare-to-none",
   "severity": "low",
   "blocking": false,
   "kind": "file",
   "fix": "Use `is None` / `is not None`."
  },
  {
   "id": "debug-leftover",
   "severity": "low",
   "blocking": false,
   "kind": "file",
   "fix": "Remove it before committing (use logging for output)."
  },
  {
   "id": "mutable-default-argument",
   "severity": "medium",
   "blocking": false,
   "kind": "file",
   "fix": "Default to None and build the container inside the function."
  },
  {
   "id": "removed-still-referenced",
   "severity": "critical",
   "blocking": true,
   "kind": "change",
   "fix": "Restore it, or update the code that still uses it in the same change."
  },
  {
   "id": "signature-break",
   "severity": "critical",
   "blocking": true,
   "kind": "change",
   "fix": "Update the call, or give the new parameter a default so existing calls keep working."
  }
 ],
 "presets": [
  {
   "id": "signature",
   "title": "Add a parameter, miss a caller",
   "blurb": "price_with_tax() gains a required state. cart.py, which nobody touched, still calls it the old way.",
   "open": "shop/prices.py",
   "before": {
    "shop/__init__.py": "",
    "shop/prices.py": "TAX_RATE = 0.08\n\n\ndef price_with_tax(amount):\n    return round(amount * (1 + TAX_RATE), 2)\n",
    "shop/cart.py": "from shop.prices import price_with_tax\n\n\ndef total(items):\n    return sum(price_with_tax(item[\"price\"]) for item in items)\n",
    "shop/checkout.py": "from shop.cart import total\n\n\ndef checkout(items, pay):\n    amount = total(items)\n    pay(amount)\n    return amount\n"
   },
   "after": {
    "shop/__init__.py": "",
    "shop/prices.py": "TAX_RATES = {\"TX\": 0.0825, \"CA\": 0.0725, \"OR\": 0.0}\n\n\ndef price_with_tax(amount, state):\n    return round(amount * (1 + TAX_RATES[state]), 2)\n",
    "shop/cart.py": "from shop.prices import price_with_tax\n\n\ndef total(items):\n    return sum(price_with_tax(item[\"price\"]) for item in items)\n",
    "shop/checkout.py": "from shop.cart import total\n\n\ndef checkout(items, pay):\n    amount = total(items)\n    pay(amount)\n    return amount\n"
   }
  },
  {
   "id": "removed",
   "title": "Delete a function that's still used",
   "blurb": "legacy_slug() looks unused and is deleted. posts.py still calls it.",
   "open": "app/text.py",
   "before": {
    "app/__init__.py": "",
    "app/text.py": "import re\n\n\ndef slugify(title):\n    return re.sub(r\"[^a-z0-9]+\", \"-\", title.lower()).strip(\"-\")\n\n\ndef legacy_slug(title):\n    return title.lower().replace(\" \", \"_\")\n",
    "app/posts.py": "from app.text import legacy_slug\n\n\ndef post_url(post):\n    return \"/posts/\" + legacy_slug(post[\"title\"])\n\n\ndef sitemap(posts):\n    return [post_url(p) for p in posts]\n"
   },
   "after": {
    "app/__init__.py": "",
    "app/text.py": "import re\n\n\ndef slugify(title):\n    return re.sub(r\"[^a-z0-9]+\", \"-\", title.lower()).strip(\"-\")\n",
    "app/posts.py": "from app.text import legacy_slug\n\n\ndef post_url(post):\n    return \"/posts/\" + legacy_slug(post[\"title\"])\n\n\ndef sitemap(posts):\n    return [post_url(p) for p in posts]\n"
   }
  },
  {
   "id": "default",
   "title": "A shared default list",
   "blurb": "A tidy-up replaces `tags=None` with `tags=[]`. Every call now shares one list.",
   "open": "tags.py",
   "before": {
    "tags.py": "def add_tag(tag, tags=None):\n    if tags is None:\n        tags = []\n    tags.append(tag)\n    return tags\n"
   },
   "after": {
    "tags.py": "def add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n"
   }
  },
  {
   "id": "leftovers",
   "title": "A quick fix, debugging left in",
   "blurb": "The team's starter rules: a bare except, a print, == None, an assert that always passes.",
   "open": "settings.py",
   "before": {
    "settings.py": "import json\n\n\ndef load_settings(path):\n    with open(path) as f:\n        return json.load(f)\n"
   },
   "after": {
    "settings.py": "import json\n\n\ndef load_settings(path):\n    try:\n        with open(path) as f:\n            settings = json.load(f)\n    except:\n        settings = {}\n    print(\"settings:\", settings)\n    if settings.get(\"theme\") == None:\n        settings[\"theme\"] = \"dark\"\n    assert (settings[\"theme\"] in (\"dark\", \"light\"), \"unknown theme\")\n    return settings\n"
   }
  },
  {
   "id": "reformat",
   "title": "Only reformatting",
   "blurb": "Spacing, quotes, a comment and a docstring. The syntax tree is the same, so nothing changed.",
   "open": "greet.py",
   "before": {
    "greet.py": "def greet(name,greeting='Hello'):\n    message=greeting+', '+name+'!'\n    return message\n"
   },
   "after": {
    "greet.py": "def greet(name, greeting=\"Hello\"):\n    \"\"\"Say hello.\"\"\"\n    message = greeting + \", \" + name + \"!\"  # tidied\n    return message\n"
   }
  },
  {
   "id": "blank",
   "title": "Your own code",
   "blurb": "Paste the old version on the left and the new one on the right.",
   "open": "app.py",
   "before": {
    "app.py": "def add(a, b):\n    return a + b\n"
   },
   "after": {
    "app.py": "def add(a, b):\n    return a + b\n"
   }
  }
 ]
};
