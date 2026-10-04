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
   "id": "leap-day-date",
   "severity": "high",
   "blocking": true,
   "kind": "file",
   "fix": "Add a timedelta instead (days=365), or handle February 29 yourself (fall back to February 28)."
  },
  {
   "id": "loop-without-progress",
   "severity": "high",
   "blocking": true,
   "kind": "file",
   "fix": "Make every way through the loop change what its condition reads, or leave the loop (break, return or raise) where it cannot."
  },
  {
   "id": "mutable-default-argument",
   "severity": "medium",
   "blocking": false,
   "kind": "file",
   "fix": "Default to None and build the container inside the function."
  },
  {
   "id": "regex-catastrophic-backtracking",
   "severity": "high",
   "blocking": false,
   "kind": "file",
   "fix": "Remove the overlap: drop redundant `.*`s, make the parts match different characters, or bound them ({0,100}); then time the pattern on a long input that does not match."
  },
  {
   "id": "removed-still-referenced",
   "severity": "critical",
   "blocking": true,
   "kind": "change",
   "fix": "Restore it, or update the code that still uses it in the same change."
  },
  {
   "id": "reused-value",
   "severity": "high",
   "blocking": false,
   "kind": "change",
   "fix": "Give the new meaning a value of its own, and retire the old one: reject it (or log it and refuse), so whatever still sends it fails loudly instead of running the new code."
  },
  {
   "id": "signature-break",
   "severity": "critical",
   "blocking": true,
   "kind": "change",
   "fix": "Update the call, or give the new parameter a default so existing calls keep working."
  },
  {
   "id": "unsynchronized-shared-state",
   "severity": "high",
   "blocking": false,
   "kind": "file",
   "fix": "Hold one lock around every read and write of the shared fields (`with self.lock:` in both methods), so one thread never sees half an update."
  }
 ],
 "examples": [
  {
   "id": "parameter",
   "title": "Add a parameter",
   "file": "shop/prices.py",
   "find": "def price_with_tax(amount):",
   "replace": "def price_with_tax(amount, state):",
   "hint": "Taxes now depend on the state. Give price_with_tax a second parameter, state.",
   "blurb": "cart.py calls price_with_tax, and checkout.py calls cart.py. Nobody will touch either of them.",
   "verdict": "block",
   "files": {
    "shop/__init__.py": "",
    "shop/prices.py": "def price_with_tax(amount):\n    return round(amount * 1.08, 2)\n",
    "shop/cart.py": "from shop.prices import price_with_tax\n\n\ndef total(prices):\n    return sum(price_with_tax(p) for p in prices)\n",
    "shop/checkout.py": "from shop.cart import total\n\n\ndef checkout(prices):\n    return f\"You pay ${total(prices)}\"\n"
   }
  },
  {
   "id": "default",
   "title": "Simplify a default",
   "file": "tags.py",
   "find": "def add_tag(tag, tags=None):",
   "replace": "def add_tag(tag, tags=[]):",
   "hint": "Looks simpler: make the default an empty list, tags=[].",
   "blurb": "A default is built once, when the function is defined, not on every call.",
   "verdict": "review",
   "files": {
    "tags.py": "def add_tag(tag, tags=None):\n    if tags is None:\n        tags = []\n    tags.append(tag)\n    return tags\n",
    "posts.py": "from tags import add_tag\n\n\ndef tag_post(post, tag):\n    post[\"tags\"] = add_tag(tag)\n    return post\n"
   }
  },
  {
   "id": "format",
   "title": "Tidy the formatting",
   "file": "greet.py",
   "find": "    message=greeting+', '+name+'!'",
   "replace": "    message = greeting + \", \" + name + \"!\"",
   "hint": "Tidy this line: spaces around the operators, double quotes.",
   "blurb": "Magellan Lite compares syntax trees, not text, so formatting is never a change.",
   "verdict": "ok",
   "files": {
    "greet.py": "def greet(name, greeting='Hello'):\n    message=greeting+', '+name+'!'\n    return message\n"
   }
  }
 ]
};
