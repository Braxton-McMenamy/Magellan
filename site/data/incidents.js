// the famous failures, replayed from demo/incidents/: written by demo/build_site.py -- do not edit by hand
self.MAGELLAN_INCIDENTS = [
 {
  "name": "azure-leap-day-2012",
  "title": "Windows Azure, February 29 2012: the leap-day outage",
  "what_happened": "The guest agent in every new VM created a transfer certificate valid until \"the same date next year\", computed by adding one to the year. On February 29 2012 that date, February 29 2013, did not exist, so certificate creation failed and the VMs never started. After three failed starts a server was assumed to be faulty hardware, its VMs were moved to other servers, where the same bug struck, and the failure cascaded until whole clusters were halted. It took 34 hours, including a second outage caused while rolling out the fix, until every service was healthy again.",
  "sources": [
   "Microsoft, \"Summary of Windows Azure Service Disruption on Feb 29th, 2012\" (March 2012)"
  ],
  "language": "Python port of the guest agent's date arithmetic.",
  "steps": [
   {
    "dir": "2-same-date-next-year",
    "what": "Valid until the same calendar date next year.",
    "verdict": "block",
    "status": "caught",
    "rules": {
     "leap-day-date": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "leap-day-date",
      "severity": "high",
      "message": "`now.replace(year=now.year + 1)` raises ValueError on February 29: a year later there is no February 29",
      "path": "fabric/certs.py",
      "line": 9,
      "detail": "It works on every other day, so every test passes. Windows Azure, 2012: certificates dated this way failed on leap day and took services down for 34 hours.",
      "fix": "Add a timedelta instead (days=365), or handle February 29 yourself (fall back to February 28)."
     }
    ],
    "changes": [
     {
      "kind": "body",
      "name": "fabric.certs.transfer_certificate_validity",
      "path": "fabric/certs.py",
      "line": 6,
      "detail": "function body changed"
     }
    ],
    "affected": [
     {
      "name": "fabric.agent.start_guest_agent",
      "path": "fabric/agent.py",
      "line": 4,
      "hops": 1,
      "score": 0.54,
      "why": "fabric.agent.start_guest_agent calls fabric.certs.transfer_certificate_validity (fabric/agent.py:5)"
     }
    ]
   }
  ],
  "date": "February 29, 2012",
  "damage": [
   {
    "value": 34,
    "suffix": " hours",
    "label": "until Microsoft declared every service healthy again (Feb 28, 4:00 PM to Mar 1, 2:15 AM PST)"
   },
   {
    "value": 33,
    "suffix": "%",
    "label": "off the month's bill for every Compute, Access Control, Service Bus and Caching customer, affected or not"
   }
  ],
  "catch": {
   "rule": "leap-day-date",
   "path": "fabric/certs.py",
   "line": 9,
   "says": "now.replace(year=now.year + 1) raises ValueError on February 29: next year has no February 29, so no certificate can be made that day.",
   "fix": "Add 365 days instead, or use February 28 when today is February 29."
  },
  "story": {
   "step": "2-same-date-next-year",
   "what": "Valid until the same calendar date next year.",
   "verdict": "block",
   "rules": {
    "leap-day-date": "caught"
   },
   "files": [
    {
     "path": "fabric/certs.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "\"\"\"Transfer certificates the guest agent creates to receive secrets from the host.\"\"\""
      ],
      [
       " ",
       2,
       ""
      ],
      [
       "-",
       null,
       "from datetime import datetime, timedelta, timezone"
      ],
      [
       "+",
       3,
       "from datetime import datetime, timezone"
      ],
      [
       " ",
       4,
       ""
      ],
      [
       " ",
       5,
       ""
      ],
      [
       " ",
       6,
       "def transfer_certificate_validity(now: datetime | None = None) -> tuple[datetime, datetime]:"
      ],
      [
       " ",
       7,
       "    \"\"\"When a new transfer certificate starts and stops being valid.\"\"\""
      ],
      [
       " ",
       8,
       "    now = now or datetime.now(timezone.utc)"
      ],
      [
       "-",
       null,
       "    return now, now + timedelta(days=365)"
      ],
      [
       "+",
       9,
       "    valid_to = now.replace(year=now.year + 1)      # the same date, one year on"
      ],
      [
       "+",
       10,
       "    return now, valid_to"
      ]
     ],
     "marks": [
      9
     ]
    }
   ],
   "changes": [
    {
     "kind": "body",
     "name": "fabric.certs.transfer_certificate_validity",
     "path": "fabric/certs.py",
     "line": 6,
     "detail": "function body changed"
    }
   ],
   "findings": [
    {
     "rule": "leap-day-date",
     "severity": "high",
     "message": "`now.replace(year=now.year + 1)` raises ValueError on February 29: a year later there is no February 29",
     "path": "fabric/certs.py",
     "line": 9,
     "detail": "It works on every other day, so every test passes. Windows Azure, 2012: certificates dated this way failed on leap day and took services down for 34 hours.",
     "fix": "Add a timedelta instead (days=365), or handle February 29 yourself (fall back to February 28)."
    }
   ],
   "affected": [
    {
     "name": "fabric.agent.start_guest_agent",
     "path": "fabric/agent.py",
     "line": 4,
     "hops": 1,
     "score": 0.54,
     "why": "fabric.agent.start_guest_agent calls fabric.certs.transfer_certificate_validity (fabric/agent.py:5)"
    }
   ],
   "map": {
    "nodes": [
     {
      "id": "fabric.agent.start_guest_agent",
      "label": "start_guest_agent",
      "path": "fabric/agent.py",
      "line": 4,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.54,
      "hops": 1,
      "finding": false
     },
     {
      "id": "fabric.certs.transfer_certificate_validity",
      "label": "transfer_certificate_validity",
      "path": "fabric/certs.py",
      "line": 6,
      "kind": "function",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": true
     }
    ],
    "edges": [
     {
      "src": "fabric.agent.start_guest_agent",
      "dst": "fabric.certs.transfer_certificate_validity",
      "kind": "calls",
      "guess": false
     }
    ]
   }
  }
 },
 {
  "name": "cloudflare-waf-2019",
  "title": "Cloudflare, July 2 2019: one regular expression exhausts the CPUs, 27-minute global outage",
  "what_happened": "An engineer deployed an update to the WAF's managed rules: a new rule against cross-site scripting, shipped in \"simulate\" mode, which only logs its matches. It blocked nothing, but its regular expression still ran on every HTTP request, and its `.*(?:.*=.*)` part backtracks super-linearly. WAF rule changes reached every server worldwide within seconds, so the CPU cores serving HTTP and HTTPS traffic everywhere went to nearly 100%, and visitors to every Cloudflare site got 502 errors. A guard that would have limited a regex's CPU use had been removed by mistake in a refactoring of the WAF weeks earlier.",
  "sources": [
   "Cloudflare blog, \"Details of the Cloudflare outage on July 2, 2019\" (July 12 2019), which quotes the regex",
   "Cloudflare blog, \"Cloudflare outage caused by bad software deploy (updated)\" (July 2 2019)"
  ],
  "language": "Python port (Cloudflare's WAF ran PCRE from Lua); the regex is verbatim.",
  "steps": [
   {
    "dir": "2-new-xss-rule",
    "what": "A new XSS rule in simulate mode, with the regex from Cloudflare's post-mortem.",
    "verdict": "review",
    "status": "caught",
    "rules": {
     "regex-catastrophic-backtracking": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "regex-catastrophic-backtracking",
      "severity": "high",
      "message": "the regular expression `(?:(?:\"|'|\\]|\\}|\\\\|\\d|(?:nan|infinity|true|fa...` can backtrack catastrophically: four unbounded parts in a row can all match the same text (a run of '+'), and what follows can fail: the work grows like n^4 with the input's length",
      "path": "waf/rules.py",
      "line": 14,
      "detail": "A request that almost matches keeps the CPU busy for seconds or more. Cloudflare, 2019: one such pattern took every server's HTTP CPUs to nearly 100% and the network down for 27 minutes.",
      "fix": "Remove the overlap: drop redundant `.*`s, make the parts match different characters, or bound them ({0,100}); then time the pattern on a long input that does not match."
     }
    ],
    "changes": [
     {
      "kind": "value",
      "name": "waf.rules.RULES",
      "path": "waf/rules.py",
      "line": 7,
      "detail": "{'sqli-union-select': ('block', re.compile('(?i)\\\\bunion\\\\b\\\\s+(?:all\\\\s+)?\\\\bselect\\\\b')), 'xss-script-tag': ('block', re.compile('(?i)<\\\\s*script\\\\b')), 'path-traversal': ('block', re.compile('(?:\\\\.\\\\./){2,}'))}  ->  {'sqli-union-select': ('block', re.compile('(?i)\\\\bunion\\\\b\\\\s+(?:all\\\\s+)?\\\\bselect\\\\b')), 'xss-script-tag': ('block', re.compile('(?i)<\\\\s*script\\\\b')), 'path-traversal': ('block', re.compile('(?:\\\\.\\\\./){2,}')), 'xss-inline-js': ('simulate', re.compile('(?:(?:\"|\\'|\\\\]|\\\\}|\\\\\\\\|\\\\d|(?:nan|infinity|true|false|null|undefined|symbol|math)|`|\\\\-|\\\\+)+[)]*;?((?:\\\\s|-|~|!|{}|\\\\|\\\\||\\\\+)*.*(?:.*=.*)))'))}"
     }
    ],
    "affected": [
     {
      "name": "waf.rules.matching_rules",
      "path": "waf/rules.py",
      "line": 18,
      "hops": 1,
      "score": 0.68,
      "why": "waf.rules.matching_rules reads waf.rules.RULES (waf/rules.py:20)"
     },
     {
      "name": "waf.edge.handle_request",
      "path": "waf/edge.py",
      "line": 7,
      "hops": 2,
      "score": 0.61,
      "why": "waf.edge.handle_request calls waf.rules.matching_rules (waf/edge.py:8)"
     }
    ]
   }
  ],
  "date": "July 2, 2019",
  "damage": [
   {
    "value": 27,
    "suffix": " min",
    "label": "of global outage, 13:42 to 14:09 UTC: Cloudflare's proxy, CDN and WAF down, and customers could not reach the dashboard or API either"
   },
   {
    "value": 82,
    "suffix": "%",
    "label": "drop in Cloudflare's traffic at the worst point"
   },
   {
    "text": "~100%",
    "label": "CPU on every core serving HTTP and HTTPS traffic, worldwide"
   }
  ],
  "catch": {
   "rule": "regex-catastrophic-backtracking",
   "path": "waf/rules.py",
   "line": 11,
   "says": "The new pattern ends in .*(?:.*=.*): three unbounded parts that can match the same characters, so on a long request the regex engine tries a super-linear number of ways to split it.",
   "fix": "Drop the redundant .* parts or bound them, and time the pattern on long inputs before it ships."
  },
  "out_of_reach": "Two things no check of this change can see: the WAF's CPU guard, removed by mistake in an earlier refactoring, and the release process that sent rule changes to every server at once. What the code shows is the regular expression itself.",
  "story": {
   "step": "2-new-xss-rule",
   "what": "A new XSS rule in simulate mode, with the regex from Cloudflare's post-mortem.",
   "verdict": "review",
   "rules": {
    "regex-catastrophic-backtracking": "caught"
   },
   "files": [
    {
     "path": "waf/rules.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "\"\"\"Managed WAF rules: each is a regular expression run against request data at the edge."
      ],
      [
       " ",
       2,
       "A rule in \"block\" mode refuses the requests it matches; a rule in \"simulate\" mode only logs"
      ],
      [
       " ",
       3,
       "them. Every rule runs on every request, whatever its mode.\"\"\""
      ],
      [
       " ",
       4,
       ""
      ],
      [
       " ",
       5,
       "import re"
      ],
      [
       " ",
       6,
       ""
      ],
      [
       " ",
       7,
       "RULES = {"
      ],
      [
       " ",
       8,
       "    \"sqli-union-select\": (\"block\", re.compile(r\"(?i)\\bunion\\b\\s+(?:all\\s+)?\\bselect\\b\")),"
      ],
      [
       " ",
       9,
       "    \"xss-script-tag\": (\"block\", re.compile(r\"(?i)<\\s*script\\b\")),"
      ],
      [
       " ",
       10,
       "    \"path-traversal\": (\"block\", re.compile(r\"(?:\\.\\./){2,}\")),"
      ],
      [
       "+",
       11,
       "    # 2019-07-02: a new rule for inline JavaScript used in XSS attacks, shipped in \"simulate\""
      ],
      [
       "+",
       12,
       "    # mode: it blocks nothing, but its regular expression runs on every request"
      ],
      [
       "+",
       13,
       "    \"xss-inline-js\": (\"simulate\", re.compile("
      ],
      [
       "+",
       14,
       "        r\"\"\"(?:(?:\"|'|\\]|\\}|\\\\|\\d|(?:nan|infinity|true|false|null|undefined|symbol|math)|`|\\-|\\+)+[)]*;?((?:\\s|-|~|!|{}|\\|\\||\\+)*.*(?:.*=.*)))\"\"\")),"
      ],
      [
       " ",
       15,
       "}"
      ],
      [
       " ",
       16,
       ""
      ],
      [
       " ",
       17,
       ""
      ],
      [
       " ",
       18,
       "def matching_rules(payload: str) -> list[tuple[str, str]]:"
      ],
      [
       " ",
       19,
       "    \"\"\"``(rule, mode)`` for every rule that matches the payload.\"\"\""
      ],
      [
       " ",
       20,
       "    return [(name, mode) for name, (mode, rule) in RULES.items() if rule.search(payload)]"
      ]
     ],
     "marks": [
      14
     ]
    }
   ],
   "changes": [
    {
     "kind": "value",
     "name": "waf.rules.RULES",
     "path": "waf/rules.py",
     "line": 7,
     "detail": "{'sqli-union-select': ('block', re.compile('(?i)\\\\bunion\\\\b\\\\s+(?:all\\\\s+)?\\\\bselect\\\\b')), 'xss-script-tag': ('block', re.compile('(?i)<\\\\s*script\\\\b')), 'path-traversal': ('block', re.compile('(?:\\\\.\\\\./){2,}'))}  ->  {'sqli-union-select': ('block', re.compile('(?i)\\\\bunion\\\\b\\\\s+(?:all\\\\s+)?\\\\bselect\\\\b')), 'xss-script-tag': ('block', re.compile('(?i)<\\\\s*script\\\\b')), 'path-traversal': ('block', re.compile('(?:\\\\.\\\\./){2,}')), 'xss-inline-js': ('simulate', re.compile('(?:(?:\"|\\'|\\\\]|\\\\}|\\\\\\\\|\\\\d|(?:nan|infinity|true|false|null|undefined|symbol|math)|`|\\\\-|\\\\+)+[)]*;?((?:\\\\s|-|~|!|{}|\\\\|\\\\||\\\\+)*.*(?:.*=.*)))'))}"
    }
   ],
   "findings": [
    {
     "rule": "regex-catastrophic-backtracking",
     "severity": "high",
     "message": "the regular expression `(?:(?:\"|'|\\]|\\}|\\\\|\\d|(?:nan|infinity|true|fa...` can backtrack catastrophically: four unbounded parts in a row can all match the same text (a run of '+'), and what follows can fail: the work grows like n^4 with the input's length",
     "path": "waf/rules.py",
     "line": 14,
     "detail": "A request that almost matches keeps the CPU busy for seconds or more. Cloudflare, 2019: one such pattern took every server's HTTP CPUs to nearly 100% and the network down for 27 minutes.",
     "fix": "Remove the overlap: drop redundant `.*`s, make the parts match different characters, or bound them ({0,100}); then time the pattern on a long input that does not match."
    }
   ],
   "affected": [
    {
     "name": "waf.rules.matching_rules",
     "path": "waf/rules.py",
     "line": 18,
     "hops": 1,
     "score": 0.68,
     "why": "waf.rules.matching_rules reads waf.rules.RULES (waf/rules.py:20)"
    },
    {
     "name": "waf.edge.handle_request",
     "path": "waf/edge.py",
     "line": 7,
     "hops": 2,
     "score": 0.61,
     "why": "waf.edge.handle_request calls waf.rules.matching_rules (waf/edge.py:8)"
    }
   ],
   "map": {
    "nodes": [
     {
      "id": "waf.edge.SIMULATED",
      "label": "SIMULATED",
      "path": "waf/edge.py",
      "line": 4,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "waf.edge.handle_request",
      "label": "handle_request",
      "path": "waf/edge.py",
      "line": 7,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.61,
      "hops": 2,
      "finding": false
     },
     {
      "id": "waf.rules.RULES",
      "label": "RULES",
      "path": "waf/rules.py",
      "line": 7,
      "kind": "constant",
      "change": "value",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": true
     },
     {
      "id": "waf.rules.matching_rules",
      "label": "matching_rules",
      "path": "waf/rules.py",
      "line": 18,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.68,
      "hops": 1,
      "finding": false
     }
    ],
    "edges": [
     {
      "src": "waf.edge.handle_request",
      "dst": "waf.rules.matching_rules",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "waf.edge.handle_request",
      "dst": "waf.edge.SIMULATED",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "waf.rules.matching_rules",
      "dst": "waf.rules.RULES",
      "kind": "reads",
      "guess": false
     }
    ]
   }
  }
 },
 {
  "name": "knight-capital-2012",
  "title": "Knight Capital, August 1 2012: $460 million in 45 minutes",
  "what_happened": "SMARS, Knight's order router, still contained 'Power Peg', unused since 2003. Power Peg sent child orders until a cumulative-fill counter said the parent order was filled; a 2005 change moved that counting earlier in the order flow, so Power Peg could no longer stop. In 2012 new Retail Liquidity Program (RLP) code replaced it and reused its flag. A technician did not copy the new code to one of eight servers; orders carrying the reused flag woke Power Peg up there and it sent millions of orders.",
  "sources": [
   "SEC Release No. 34-70694, In the Matter of Knight Capital Americas LLC (2013)",
   "SEC press release 2013-222, \"SEC Charges Knight Capital With Violations of Market Access Rule\" (October 16 2013)"
  ],
  "language": "Python port (SMARS's source and language are not public).",
  "steps": [
   {
    "dir": "2-2005-fill-tracking-moved",
    "what": "Fill tracking moves out of Power Peg's loop to the start of routing.",
    "verdict": "block",
    "status": "caught",
    "rules": {
     "loop-without-progress": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "loop-without-progress",
      "severity": "high",
      "message": "this loop never ends once it starts: nothing in it changes order.filled or order.qty, and nothing leaves it",
      "path": "smars/power_peg.py",
      "line": 11,
      "detail": "Once it runs, the program hangs or keeps doing the same thing forever. Knight Capital, 2012: a loop like this sent orders without end ($460 million in 45 minutes). Zune, 2008: one like it froze every Zune 30 on the last day of a leap year.",
      "fix": "Make every way through the loop change what its condition reads, or leave the loop (break, return or raise) where it cannot."
     }
    ],
    "changes": [
     {
      "kind": "body",
      "name": "smars.power_peg.power_peg",
      "path": "smars/power_peg.py",
      "line": 9,
      "detail": "function body changed"
     },
     {
      "kind": "body",
      "name": "smars.router.route",
      "path": "smars/router.py",
      "line": 7,
      "detail": "function body changed"
     }
    ],
    "affected": [
     {
      "name": "smars.main.on_parent_order",
      "path": "smars/main.py",
      "line": 5,
      "hops": 1,
      "score": 0.54,
      "why": "smars.main.on_parent_order calls smars.router.route (smars/main.py:7)"
     }
    ]
   },
   {
    "dir": "3-2012-rlp-reuses-the-flag",
    "what": "RLP replaces Power Peg and takes over its flag value 0x08.",
    "verdict": "review",
    "status": "caught",
    "rules": {
     "reused-value": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "reused-value",
      "severity": "high",
      "message": "RLP takes over the value 0x08 that POWER_PEG had: route() used to call power_peg() and track_cumulative() for it, and now calls rlp()",
      "path": "smars/router.py",
      "line": 8,
      "detail": "Anything that still sends, stores or runs with the old meaning of 0x08 -- another service, saved data, a server still on the previous build -- now gets the new behaviour. Knight Capital, 2012: a reused flag woke dead code on one server and lost $460 million in 45 minutes.",
      "fix": "Give the new meaning a value of its own, and retire the old one: reject it (or log it and refuse), so whatever still sends it fails loudly instead of running the new code."
     }
    ],
    "changes": [
     {
      "kind": "removed",
      "name": "smars.flags.POWER_PEG",
      "path": "smars/flags.py",
      "line": 1,
      "detail": "constant deleted"
     },
     {
      "kind": "removed",
      "name": "smars.power_peg.track_cumulative",
      "path": "smars/power_peg.py",
      "line": 5,
      "detail": "function deleted"
     },
     {
      "kind": "removed",
      "name": "smars.power_peg.power_peg",
      "path": "smars/power_peg.py",
      "line": 9,
      "detail": "function deleted"
     },
     {
      "kind": "body",
      "name": "smars.router.route",
      "path": "smars/router.py",
      "line": 7,
      "detail": "function body changed"
     },
     {
      "kind": "added",
      "name": "smars.flags.RLP",
      "path": "smars/flags.py",
      "line": 1,
      "detail": "new constant"
     },
     {
      "kind": "added",
      "name": "smars.rlp.rlp",
      "path": "smars/rlp.py",
      "line": 5,
      "detail": "new function"
     }
    ],
    "affected": [
     {
      "name": "smars.main.on_parent_order",
      "path": "smars/main.py",
      "line": 5,
      "hops": 1,
      "score": 0.54,
      "why": "smars.main.on_parent_order calls smars.router.route (smars/main.py:7)"
     }
    ]
   }
  ],
  "date": "August 1, 2012",
  "damage": [
   {
    "value": 460,
    "prefix": "$",
    "suffix": "M",
    "label": "lost: \"Knight lost over $460 million\" (SEC)"
   },
   {
    "value": 45,
    "suffix": " min",
    "label": "the first 45 minutes after the market opened, routing millions of orders"
   },
   {
    "value": 397,
    "suffix": "M",
    "label": "shares traded that nobody meant to trade"
   }
  ],
  "catch": {
   "rule": "loop-without-progress",
   "path": "smars/power_peg.py",
   "line": 11,
   "says": "while order.filled < order.qty: nothing in the loop changes order.filled any more (the counting moved to route), so once Power Peg starts it never stops.",
   "fix": "Count fills inside the loop again, or cap how many child orders it may send."
  },
  "out_of_reach": "The deployment that skipped one of eight servers is a release failure no source analysis can see; what the code shows is the loop that cannot stop and the flag handed to a new meaning.",
  "story": {
   "step": "2-2005-fill-tracking-moved",
   "what": "Fill tracking moves out of Power Peg's loop to the start of routing.",
   "verdict": "block",
   "rules": {
    "loop-without-progress": "caught"
   },
   "files": [
    {
     "path": "smars/power_peg.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "from smars.market import send_child"
      ],
      [
       " ",
       2,
       "from smars.orders import ParentOrder"
      ],
      [
       " ",
       3,
       ""
      ],
      [
       " ",
       4,
       ""
      ],
      [
       " ",
       5,
       "def track_cumulative(order: ParentOrder, filled: int) -> None:"
      ],
      [
       " ",
       6,
       "    order.filled += filled"
      ],
      [
       " ",
       7,
       ""
      ],
      [
       " ",
       8,
       ""
      ],
      [
       " ",
       9,
       "def power_peg(order: ParentOrder) -> None:"
      ],
      [
       " ",
       10,
       "    \"\"\"Keep sending child orders until the parent order is filled.\"\"\""
      ],
      [
       " ",
       11,
       "    while order.filled < order.qty:"
      ],
      [
       "-",
       null,
       "        filled = send_child(order.symbol, order.qty - order.filled)"
      ],
      [
       "-",
       null,
       "        track_cumulative(order, filled)"
      ],
      [
       "+",
       12,
       "        send_child(order.symbol, order.qty - order.filled)"
      ]
     ],
     "marks": [
      11
     ]
    },
    {
     "path": "smars/router.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "from smars.flags import POWER_PEG"
      ],
      [
       " ",
       2,
       "from smars.market import send_child"
      ],
      [
       " ",
       3,
       "from smars.orders import ParentOrder"
      ],
      [
       "-",
       null,
       "from smars.power_peg import power_peg"
      ],
      [
       "+",
       4,
       "from smars.power_peg import power_peg, track_cumulative"
      ],
      [
       " ",
       5,
       ""
      ],
      [
       " ",
       6,
       ""
      ],
      [
       " ",
       7,
       "def route(order: ParentOrder) -> None:"
      ],
      [
       "+",
       8,
       "    track_cumulative(order, 0)"
      ],
      [
       " ",
       9,
       "    if order.flags & POWER_PEG:"
      ],
      [
       " ",
       10,
       "        power_peg(order)"
      ],
      [
       " ",
       11,
       "    else:"
      ],
      [
       " ",
       12,
       "        order.filled += send_child(order.symbol, order.qty)"
      ]
     ],
     "marks": []
    }
   ],
   "changes": [
    {
     "kind": "body",
     "name": "smars.power_peg.power_peg",
     "path": "smars/power_peg.py",
     "line": 9,
     "detail": "function body changed"
    },
    {
     "kind": "body",
     "name": "smars.router.route",
     "path": "smars/router.py",
     "line": 7,
     "detail": "function body changed"
    }
   ],
   "findings": [
    {
     "rule": "loop-without-progress",
     "severity": "high",
     "message": "this loop never ends once it starts: nothing in it changes order.filled or order.qty, and nothing leaves it",
     "path": "smars/power_peg.py",
     "line": 11,
     "detail": "Once it runs, the program hangs or keeps doing the same thing forever. Knight Capital, 2012: a loop like this sent orders without end ($460 million in 45 minutes). Zune, 2008: one like it froze every Zune 30 on the last day of a leap year.",
     "fix": "Make every way through the loop change what its condition reads, or leave the loop (break, return or raise) where it cannot."
    }
   ],
   "affected": [
    {
     "name": "smars.main.on_parent_order",
     "path": "smars/main.py",
     "line": 5,
     "hops": 1,
     "score": 0.54,
     "why": "smars.main.on_parent_order calls smars.router.route (smars/main.py:7)"
    }
   ],
   "map": {
    "nodes": [
     {
      "id": "smars.flags.POWER_PEG",
      "label": "POWER_PEG",
      "path": "smars/flags.py",
      "line": 1,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.main.on_parent_order",
      "label": "on_parent_order",
      "path": "smars/main.py",
      "line": 5,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.54,
      "hops": 1,
      "finding": false
     },
     {
      "id": "smars.market.send_child",
      "label": "send_child",
      "path": "smars/market.py",
      "line": 1,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.orders.ParentOrder",
      "label": "ParentOrder",
      "path": "smars/orders.py",
      "line": 5,
      "kind": "class",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.orders.ParentOrder.filled",
      "label": "ParentOrder.filled",
      "path": "smars/orders.py",
      "line": 9,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.orders.ParentOrder.flags",
      "label": "ParentOrder.flags",
      "path": "smars/orders.py",
      "line": 8,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.power_peg.power_peg",
      "label": "power_peg",
      "path": "smars/power_peg.py",
      "line": 9,
      "kind": "function",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": true
     },
     {
      "id": "smars.power_peg.track_cumulative",
      "label": "track_cumulative",
      "path": "smars/power_peg.py",
      "line": 5,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "smars.router.route",
      "label": "route",
      "path": "smars/router.py",
      "line": 7,
      "kind": "function",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     }
    ],
    "edges": [
     {
      "src": "smars.main.on_parent_order",
      "dst": "smars.orders.ParentOrder",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "smars.main.on_parent_order",
      "dst": "smars.router.route",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "smars.power_peg.power_peg",
      "dst": "smars.market.send_child",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "smars.router.route",
      "dst": "smars.power_peg.track_cumulative",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "smars.router.route",
      "dst": "smars.flags.POWER_PEG",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "smars.router.route",
      "dst": "smars.power_peg.power_peg",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "smars.router.route",
      "dst": "smars.market.send_child",
      "kind": "calls",
      "guess": false
     }
    ]
   }
  }
 },
 {
  "name": "sensor-signature-break",
  "title": "The sensor agent: a signature change breaks a file nobody touched",
  "what_happened": "A three-file change adds a required `layout` argument to parse_record and drops the legacy collection mode. Every edited file is consistent. But collector.py, which nobody touched, still calls parse_record(row) with one argument -- inside `except Exception: pass`, so the TypeError is swallowed and the sweep silently returns nothing.",
  "sources": [
   "Magellan's worked example (examples/sensor and examples/after)",
   "CrowdStrike, \"External Technical Root Cause Analysis: Channel File 291\" (August 6 2024)",
   "Microsoft, \"Helping our customers through the CrowdStrike outage\" (July 20 2024)",
   "Parametrix, CrowdStrike outage impact analysis of the US Fortune 500 (July 24 2024)"
  ],
  "language": "Python",
  "steps": [
   {
    "dir": "2-layout-argument",
    "what": "parse_record gains a required layout argument; collector.py is not updated.",
    "verdict": "block",
    "status": "caught",
    "rules": {
     "signature-break": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "signature-break",
      "severity": "critical",
      "message": "sensor.collector.collect calls parse_record() the old way: it now requires layout, which the call does not pass",
      "path": "sensor/collector.py",
      "line": 11,
      "detail": "sensor.channel.parse_record went from (fields: list[str]) -> dict[str, str] to (fields: list[str], layout: str) -> dict[str, str]. It is in code this change did not touch, so nobody updated it: the call raises TypeError when it runs.",
      "fix": "Update the call, or give the new parameter a default so existing calls keep working."
     }
    ],
    "changes": [
     {
      "kind": "removed",
      "name": "sensor.api.handle_legacy_upload",
      "path": "sensor/api.py",
      "line": 16,
      "detail": "function deleted"
     },
     {
      "kind": "removed",
      "name": "sensor.api.weigh_one",
      "path": "sensor/api.py",
      "line": 21,
      "detail": "function deleted"
     },
     {
      "kind": "removed",
      "name": "sensor.pipeline.process_legacy",
      "path": "sensor/pipeline.py",
      "line": 40,
      "detail": "function deleted"
     },
     {
      "kind": "signature",
      "name": "sensor.channel.parse_record",
      "path": "sensor/channel.py",
      "line": 7,
      "detail": "(fields: list[str]) -> dict[str, str]  ->  (fields: list[str], layout: str) -> dict[str, str]"
     },
     {
      "kind": "body",
      "name": "sensor.pipeline.process",
      "path": "sensor/pipeline.py",
      "line": 27,
      "detail": "function body changed"
     },
     {
      "kind": "added",
      "name": "sensor.channel.LAYOUT_V3",
      "path": "sensor/channel.py",
      "line": 3,
      "detail": "new constant"
     },
     {
      "kind": "added",
      "name": "sensor.channel.LAYOUT_V4",
      "path": "sensor/channel.py",
      "line": 4,
      "detail": "new constant"
     }
    ],
    "affected": [
     {
      "name": "sensor.collector.collect",
      "path": "sensor/collector.py",
      "line": 7,
      "hops": 1,
      "score": 0.85,
      "why": "sensor.collector.collect calls sensor.channel.parse_record (sensor/collector.py:11)"
     },
     {
      "name": "sensor.collector.sweep",
      "path": "sensor/collector.py",
      "line": 17,
      "hops": 2,
      "score": 0.77,
      "why": "sensor.collector.sweep calls sensor.collector.collect (sensor/collector.py:18)"
     },
     {
      "name": "sensor.main.run_agent",
      "path": "sensor/main.py",
      "line": 8,
      "hops": 3,
      "score": 0.69,
      "why": "sensor.main.run_agent calls sensor.collector.sweep (sensor/main.py:10)"
     },
     {
      "name": "sensor.api.handle_fast_upload",
      "path": "sensor/api.py",
      "line": 12,
      "hops": 1,
      "score": 0.54,
      "why": "sensor.api.handle_fast_upload calls sensor.pipeline.process (sensor/api.py:13)"
     },
     {
      "name": "sensor.api.handle_upload",
      "path": "sensor/api.py",
      "line": 8,
      "hops": 1,
      "score": 0.54,
      "why": "sensor.api.handle_upload calls sensor.pipeline.process (sensor/api.py:9)"
     },
     {
      "name": "sensor.pipeline.process_fast",
      "path": "sensor/pipeline.py",
      "line": 36,
      "hops": 1,
      "score": 0.54,
      "why": "sensor.pipeline.process_fast calls sensor.pipeline.process (sensor/pipeline.py:37)"
     }
    ]
   }
  ],
  "damage": [
   {
    "value": 8.5,
    "suffix": "M",
    "label": "Windows devices affected (Microsoft's estimate)"
   },
   {
    "value": 5.4,
    "prefix": "$",
    "suffix": "B",
    "label": "estimated direct losses for US Fortune 500 companies alone (Parametrix)"
   }
  ],
  "damage_of": "CrowdStrike, July 19 2024, the real failure of this kind: the sensor's IPC template type defined 21 input fields, but the code that called it supplied only 20. A content update that used the 21st made the sensor read past the end of its input, and Windows crashed.",
  "catch": {
   "rule": "signature-break",
   "path": "sensor/collector.py",
   "line": 11,
   "says": "collect() still calls parse_record(row) with one argument; parse_record now requires layout. The TypeError is swallowed by except Exception: pass, so the sweep silently returns nothing.",
   "fix": "Update the call, or give layout a default so existing calls keep working."
  },
  "story": {
   "step": "2-layout-argument",
   "what": "parse_record gains a required layout argument; collector.py is not updated.",
   "verdict": "block",
   "rules": {
    "signature-break": "caught"
   },
   "files": [
    {
     "path": "sensor/channel.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "\"\"\"Parsing of channel files handed to us by the update service.\"\"\""
      ],
      [
       " ",
       2,
       ""
      ],
      [
       "+",
       3,
       "LAYOUT_V3 = \"v3\""
      ],
      [
       "+",
       4,
       "LAYOUT_V4 = \"v4\""
      ],
      [
       " ",
       5,
       ""
      ],
      [
       "-",
       null,
       "def parse_record(fields: list[str]) -> dict[str, str]:"
      ],
      [
       "+",
       6,
       ""
      ],
      [
       "+",
       7,
       "def parse_record(fields: list[str], layout: str) -> dict[str, str]:"
      ],
      [
       " ",
       8,
       "    \"\"\"Turn one raw channel row into a record."
      ],
      [
       " ",
       9,
       ""
      ],
      [
       "-",
       null,
       "    The row layout is fixed by the update service, so positions are read"
      ],
      [
       "-",
       null,
       "    directly."
      ],
      [
       "+",
       10,
       "    The update service now ships two layouts, so the caller states which one it"
      ],
      [
       "+",
       11,
       "    is handing us."
      ],
      [
       " ",
       12,
       "    \"\"\""
      ],
      [
       "+",
       13,
       "    if layout == LAYOUT_V4:"
      ],
      [
       "+",
       14,
       "        return {"
      ],
      [
       "+",
       15,
       "            \"kind\": fields[0],"
      ],
      [
       "+",
       16,
       "            \"host\": fields[1],"
      ],
      [
       "+",
       17,
       "            \"value\": fields[5],"
      ],
      [
       "+",
       18,
       "        }"
      ],
      [
       " ",
       19,
       "    return {"
      ],
      [
       " ",
       20,
       "        \"kind\": fields[0],"
      ],
      [
       " ",
       21,
       "        \"host\": fields[1],"
      ],
      [
       " ",
       22,
       "        \"value\": fields[4],"
      ],
      [
       " ",
       23,
       "    }"
      ],
      [
       " ",
       24,
       ""
      ],
      [
       " ",
       25,
       ""
      ],
      [
       " ",
       26,
       "def split_rows(blob: str) -> list[list[str]]:"
      ],
      [
       " ",
       27,
       "    return [line.split(\",\") for line in blob.splitlines() if line]"
      ]
     ],
     "marks": []
    },
    {
     "path": "sensor/collector.py",
     "state": "untouched",
     "lines": [
      [
       " ",
       1,
       "\"\"\"The scheduled sweep path. Shares the channel parser with the upload path.\"\"\""
      ],
      [
       " ",
       2,
       ""
      ],
      [
       " ",
       3,
       "from sensor.channel import parse_record, split_rows"
      ],
      [
       " ",
       4,
       "from sensor.registry import note_error"
      ],
      [
       " ",
       5,
       ""
      ],
      [
       " ",
       6,
       ""
      ],
      [
       " ",
       7,
       "def collect(blob: str) -> list[dict[str, str]]:"
      ],
      [
       " ",
       8,
       "    records = []"
      ],
      [
       " ",
       9,
       "    for row in split_rows(blob):"
      ],
      [
       " ",
       10,
       "        try:"
      ],
      [
       " ",
       11,
       "            records.append(parse_record(row))"
      ],
      [
       " ",
       12,
       "        except Exception:"
      ],
      [
       " ",
       13,
       "            pass"
      ],
      [
       " ",
       14,
       "    return records"
      ],
      [
       " ",
       15,
       ""
      ],
      [
       " ",
       16,
       ""
      ],
      [
       " ",
       17,
       "def sweep(blob: str) -> int:"
      ],
      [
       " ",
       18,
       "    records = collect(blob)"
      ],
      [
       " ",
       19,
       "    if not records:"
      ],
      [
       " ",
       20,
       "        note_error(\"empty sweep\")"
      ],
      [
       " ",
       21,
       "    return len(records)"
      ]
     ],
     "marks": [
      11
     ]
    }
   ],
   "changes": [
    {
     "kind": "removed",
     "name": "sensor.api.handle_legacy_upload",
     "path": "sensor/api.py",
     "line": 16,
     "detail": "function deleted"
    },
    {
     "kind": "removed",
     "name": "sensor.api.weigh_one",
     "path": "sensor/api.py",
     "line": 21,
     "detail": "function deleted"
    },
    {
     "kind": "removed",
     "name": "sensor.pipeline.process_legacy",
     "path": "sensor/pipeline.py",
     "line": 40,
     "detail": "function deleted"
    },
    {
     "kind": "signature",
     "name": "sensor.channel.parse_record",
     "path": "sensor/channel.py",
     "line": 7,
     "detail": "(fields: list[str]) -> dict[str, str]  ->  (fields: list[str], layout: str) -> dict[str, str]"
    },
    {
     "kind": "body",
     "name": "sensor.pipeline.process",
     "path": "sensor/pipeline.py",
     "line": 27,
     "detail": "function body changed"
    },
    {
     "kind": "added",
     "name": "sensor.channel.LAYOUT_V3",
     "path": "sensor/channel.py",
     "line": 3,
     "detail": "new constant"
    },
    {
     "kind": "added",
     "name": "sensor.channel.LAYOUT_V4",
     "path": "sensor/channel.py",
     "line": 4,
     "detail": "new constant"
    }
   ],
   "findings": [
    {
     "rule": "signature-break",
     "severity": "critical",
     "message": "sensor.collector.collect calls parse_record() the old way: it now requires layout, which the call does not pass",
     "path": "sensor/collector.py",
     "line": 11,
     "detail": "sensor.channel.parse_record went from (fields: list[str]) -> dict[str, str] to (fields: list[str], layout: str) -> dict[str, str]. It is in code this change did not touch, so nobody updated it: the call raises TypeError when it runs.",
     "fix": "Update the call, or give the new parameter a default so existing calls keep working."
    }
   ],
   "affected": [
    {
     "name": "sensor.collector.collect",
     "path": "sensor/collector.py",
     "line": 7,
     "hops": 1,
     "score": 0.85,
     "why": "sensor.collector.collect calls sensor.channel.parse_record (sensor/collector.py:11)"
    },
    {
     "name": "sensor.collector.sweep",
     "path": "sensor/collector.py",
     "line": 17,
     "hops": 2,
     "score": 0.77,
     "why": "sensor.collector.sweep calls sensor.collector.collect (sensor/collector.py:18)"
    },
    {
     "name": "sensor.main.run_agent",
     "path": "sensor/main.py",
     "line": 8,
     "hops": 3,
     "score": 0.69,
     "why": "sensor.main.run_agent calls sensor.collector.sweep (sensor/main.py:10)"
    },
    {
     "name": "sensor.api.handle_fast_upload",
     "path": "sensor/api.py",
     "line": 12,
     "hops": 1,
     "score": 0.54,
     "why": "sensor.api.handle_fast_upload calls sensor.pipeline.process (sensor/api.py:13)"
    },
    {
     "name": "sensor.api.handle_upload",
     "path": "sensor/api.py",
     "line": 8,
     "hops": 1,
     "score": 0.54,
     "why": "sensor.api.handle_upload calls sensor.pipeline.process (sensor/api.py:9)"
    },
    {
     "name": "sensor.pipeline.process_fast",
     "path": "sensor/pipeline.py",
     "line": 36,
     "hops": 1,
     "score": 0.54,
     "why": "sensor.pipeline.process_fast calls sensor.pipeline.process (sensor/pipeline.py:37)"
    }
   ],
   "map": {
    "nodes": [
     {
      "id": "sensor.__all__",
      "label": "__all__",
      "path": "sensor/__init__.py",
      "line": 3,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.api.handle_fast_upload",
      "label": "handle_fast_upload",
      "path": "sensor/api.py",
      "line": 12,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.54,
      "hops": 1,
      "finding": false
     },
     {
      "id": "sensor.api.handle_legacy_upload",
      "label": "handle_legacy_upload",
      "path": "sensor/api.py",
      "line": 16,
      "kind": "function",
      "change": "removed",
      "removed": true,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.api.handle_upload",
      "label": "handle_upload",
      "path": "sensor/api.py",
      "line": 8,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.54,
      "hops": 1,
      "finding": false
     },
     {
      "id": "sensor.api.weigh_one",
      "label": "weigh_one",
      "path": "sensor/api.py",
      "line": 21,
      "kind": "function",
      "change": "removed",
      "removed": true,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.channel.LAYOUT_V3",
      "label": "LAYOUT_V3",
      "path": "sensor/channel.py",
      "line": 3,
      "kind": "constant",
      "change": "added",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.channel.LAYOUT_V4",
      "label": "LAYOUT_V4",
      "path": "sensor/channel.py",
      "line": 4,
      "kind": "constant",
      "change": "added",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.channel.parse_record",
      "label": "parse_record",
      "path": "sensor/channel.py",
      "line": 7,
      "kind": "function",
      "change": "signature",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.channel.split_rows",
      "label": "split_rows",
      "path": "sensor/channel.py",
      "line": 26,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.collector.collect",
      "label": "collect",
      "path": "sensor/collector.py",
      "line": 7,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.85,
      "hops": 1,
      "finding": true
     },
     {
      "id": "sensor.collector.sweep",
      "label": "sweep",
      "path": "sensor/collector.py",
      "line": 17,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.77,
      "hops": 2,
      "finding": false
     },
     {
      "id": "sensor.config.DEFAULT_MODE",
      "label": "DEFAULT_MODE",
      "path": "sensor/config.py",
      "line": 7,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.config.MAX_BATCH",
      "label": "MAX_BATCH",
      "path": "sensor/config.py",
      "line": 8,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.config.MODE_FAST",
      "label": "MODE_FAST",
      "path": "sensor/config.py",
      "line": 3,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.config.MODE_LEGACY",
      "label": "MODE_LEGACY",
      "path": "sensor/config.py",
      "line": 5,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.config.MODE_SAFE",
      "label": "MODE_SAFE",
      "path": "sensor/config.py",
      "line": 4,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.main.run_agent",
      "label": "run_agent",
      "path": "sensor/main.py",
      "line": 8,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.69,
      "hops": 3,
      "finding": false
     },
     {
      "id": "sensor.pipeline._dispatch",
      "label": "_dispatch",
      "path": "sensor/pipeline.py",
      "line": 8,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.pipeline._legacy_weight",
      "label": "_legacy_weight",
      "path": "sensor/pipeline.py",
      "line": 22,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.pipeline.process",
      "label": "process",
      "path": "sensor/pipeline.py",
      "line": 27,
      "kind": "function",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.pipeline.process_fast",
      "label": "process_fast",
      "path": "sensor/pipeline.py",
      "line": 36,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0.54,
      "hops": 1,
      "finding": false
     },
     {
      "id": "sensor.pipeline.process_legacy",
      "label": "process_legacy",
      "path": "sensor/pipeline.py",
      "line": 40,
      "kind": "function",
      "change": "removed",
      "removed": true,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.registry._ERRORS",
      "label": "_ERRORS",
      "path": "sensor/registry.py",
      "line": 4,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.registry._SEEN",
      "label": "_SEEN",
      "path": "sensor/registry.py",
      "line": 3,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.registry.note_error",
      "label": "note_error",
      "path": "sensor/registry.py",
      "line": 12,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.registry.remember",
      "label": "remember",
      "path": "sensor/registry.py",
      "line": 7,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "sensor.registry.seen_count",
      "label": "seen_count",
      "path": "sensor/registry.py",
      "line": 16,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     }
    ],
    "edges": [
     {
      "src": "sensor.api.handle_upload",
      "dst": "sensor.pipeline.process",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.api.handle_upload",
      "dst": "sensor.registry.seen_count",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.api.handle_upload",
      "dst": "sensor.config.MODE_SAFE",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.api.handle_fast_upload",
      "dst": "sensor.pipeline.process",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.api.handle_fast_upload",
      "dst": "sensor.registry.seen_count",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.api.handle_fast_upload",
      "dst": "sensor.config.MODE_FAST",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.channel.parse_record",
      "dst": "sensor.channel.LAYOUT_V4",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.collector.collect",
      "dst": "sensor.channel.split_rows",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.collector.collect",
      "dst": "sensor.channel.parse_record",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.collector.sweep",
      "dst": "sensor.collector.collect",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.collector.sweep",
      "dst": "sensor.registry.note_error",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.main.run_agent",
      "dst": "sensor.api.handle_upload",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.main.run_agent",
      "dst": "sensor.collector.sweep",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline._dispatch",
      "dst": "sensor.pipeline._legacy_weight",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline._dispatch",
      "dst": "sensor.registry.note_error",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.config.DEFAULT_MODE",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.channel.split_rows",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.channel.parse_record",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.pipeline._dispatch",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.registry.remember",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.config.MAX_BATCH",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process",
      "dst": "sensor.channel.LAYOUT_V4",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process_fast",
      "dst": "sensor.pipeline.process",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "sensor.pipeline.process_fast",
      "dst": "sensor.config.MODE_FAST",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.registry.remember",
      "dst": "sensor.registry._SEEN",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.registry.note_error",
      "dst": "sensor.registry._ERRORS",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "sensor.registry.seen_count",
      "dst": "sensor.registry._SEEN",
      "kind": "reads",
      "guess": false
     }
    ]
   }
  }
 },
 {
  "name": "therac-25-1986",
  "title": "Therac-25, 1985-1987: radiation overdoses from a race condition",
  "what_happened": "The operator could edit the prescription (beam mode and energy) while the treatment task was still setting the bending magnets, which took about eight seconds. The two tasks shared those values without synchronisation, so a quick correction from X-ray to electron mode was not fully seen by the setup task: the machine fired the high-current beam meant for X-ray mode without the X-ray target in its path, about 100 times the intended dose. The Therac-20 had hardware interlocks that caught this; the Therac-25 relied on software alone. At least six accidents; three patients died of their overdoses.",
  "sources": [
   "Nancy Leveson and Clark Turner, \"An Investigation of the Therac-25 Accidents\", IEEE Computer (1993)"
  ],
  "language": "Python port (the original was PDP-11 assembly).",
  "steps": [
   {
    "dir": "2-software-only",
    "what": "The serialisation is gone: an edit can land while setup reads the old values.",
    "verdict": "review",
    "status": "caught",
    "rules": {
     "unsynchronized-shared-state": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "unsynchronized-shared-state",
      "severity": "high",
      "message": "operator_edit() writes self.energy_mev and self.mode while set_up_beam() uses them on another thread, with no lock held",
      "path": "therac/console.py",
      "line": 14,
      "detail": "Both are started as threads (lines 22 and 23), so set_up_beam() can run halfway through operator_edit()'s update and see a mix of old and new values. Therac-25: a quick edit was half seen by the setup task, and patients received about 100 times the intended dose.",
      "fix": "Hold one lock around every read and write of the shared fields (`with self.lock:` in both methods), so one thread never sees half an update."
     }
    ],
    "changes": [
     {
      "kind": "body",
      "name": "therac.console.TreatmentConsole.operator_edit",
      "path": "therac/console.py",
      "line": 12,
      "detail": "method body changed"
     },
     {
      "kind": "body",
      "name": "therac.console.TreatmentConsole.set_up_beam",
      "path": "therac/console.py",
      "line": 17,
      "detail": "method body changed"
     }
    ],
    "affected": []
   }
  ],
  "date": "1985 to 1987",
  "damage": [
   {
    "value": 6,
    "label": "known accidents, 1985 to 1987: patients given massive radiation overdoses"
   },
   {
    "value": 3,
    "label": "patients died of their overdoses"
   }
  ],
  "damage_note": "The cost was human. No money figure fits it.",
  "catch": {
   "rule": "unsynchronized-shared-state",
   "path": "therac/console.py",
   "line": 14,
   "says": "operator_edit writes self.mode and self.energy_mev on one thread while set_up_beam reads them on another, and neither takes self.lock: an edit can land halfway through setup.",
   "fix": "Take self.lock in both methods (with self.lock:), so an edit and the setup never interleave."
  },
  "out_of_reach": "The hardware interlock that was removed is not code; what is visible is the shared state with no lock between the two tasks.",
  "story": {
   "step": "2-software-only",
   "what": "The serialisation is gone: an edit can land while setup reads the old values.",
   "verdict": "review",
   "rules": {
    "unsynchronized-shared-state": "caught"
   },
   "files": [
    {
     "path": "therac/console.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "\"\"\"Treatment console: the operator enters the beam mode and energy, the setup task reads them.\"\"\""
      ],
      [
       " ",
       2,
       ""
      ],
      [
       " ",
       3,
       "import threading"
      ],
      [
       " ",
       4,
       ""
      ],
      [
       " ",
       5,
       ""
      ],
      [
       " ",
       6,
       "class TreatmentConsole:"
      ],
      [
       " ",
       7,
       "    def __init__(self) -> None:"
      ],
      [
       " ",
       8,
       "        self.lock = threading.Lock()"
      ],
      [
       " ",
       9,
       "        self.mode = \"xray\""
      ],
      [
       " ",
       10,
       "        self.energy_mev = 25"
      ],
      [
       " ",
       11,
       ""
      ],
      [
       " ",
       12,
       "    def operator_edit(self, mode: str, energy_mev: int) -> None:"
      ],
      [
       " ",
       13,
       "        \"\"\"Keyboard handler: the operator corrects the prescription.\"\"\""
      ],
      [
       "-",
       null,
       "        with self.lock:"
      ],
      [
       "-",
       null,
       "            self.mode = mode"
      ],
      [
       "-",
       null,
       "            self.energy_mev = energy_mev"
      ],
      [
       "+",
       14,
       "        self.mode = mode"
      ],
      [
       "+",
       15,
       "        self.energy_mev = energy_mev"
      ],
      [
       " ",
       16,
       ""
      ],
      [
       " ",
       17,
       "    def set_up_beam(self) -> tuple[str, int]:"
      ],
      [
       " ",
       18,
       "        \"\"\"Treatment task: position the bending magnets for what was entered.\"\"\""
      ],
      [
       "-",
       null,
       "        with self.lock:"
      ],
      [
       "-",
       null,
       "            return self.mode, self.energy_mev"
      ],
      [
       "+",
       19,
       "        return self.mode, self.energy_mev"
      ],
      [
       " ",
       20,
       ""
      ],
      [
       " ",
       21,
       "    def start(self, mode: str, energy_mev: int) -> None:"
      ],
      [
       " ",
       22,
       "        threading.Thread(target=self.operator_edit, args=(mode, energy_mev)).start()"
      ],
      [
       " ",
       23,
       "        threading.Thread(target=self.set_up_beam).start()"
      ]
     ],
     "marks": [
      14
     ]
    }
   ],
   "changes": [
    {
     "kind": "body",
     "name": "therac.console.TreatmentConsole.operator_edit",
     "path": "therac/console.py",
     "line": 12,
     "detail": "method body changed"
    },
    {
     "kind": "body",
     "name": "therac.console.TreatmentConsole.set_up_beam",
     "path": "therac/console.py",
     "line": 17,
     "detail": "method body changed"
    }
   ],
   "findings": [
    {
     "rule": "unsynchronized-shared-state",
     "severity": "high",
     "message": "operator_edit() writes self.energy_mev and self.mode while set_up_beam() uses them on another thread, with no lock held",
     "path": "therac/console.py",
     "line": 14,
     "detail": "Both are started as threads (lines 22 and 23), so set_up_beam() can run halfway through operator_edit()'s update and see a mix of old and new values. Therac-25: a quick edit was half seen by the setup task, and patients received about 100 times the intended dose.",
     "fix": "Hold one lock around every read and write of the shared fields (`with self.lock:` in both methods), so one thread never sees half an update."
    }
   ],
   "affected": [],
   "map": {
    "nodes": [
     {
      "id": "therac.console.TreatmentConsole",
      "label": "TreatmentConsole",
      "path": "therac/console.py",
      "line": 6,
      "kind": "class",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "therac.console.TreatmentConsole.__init__",
      "label": "TreatmentConsole.__init__",
      "path": "therac/console.py",
      "line": 7,
      "kind": "method",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "therac.console.TreatmentConsole.operator_edit",
      "label": "TreatmentConsole.operator_edit",
      "path": "therac/console.py",
      "line": 12,
      "kind": "method",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": true
     },
     {
      "id": "therac.console.TreatmentConsole.set_up_beam",
      "label": "TreatmentConsole.set_up_beam",
      "path": "therac/console.py",
      "line": 17,
      "kind": "method",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "therac.console.TreatmentConsole.start",
      "label": "TreatmentConsole.start",
      "path": "therac/console.py",
      "line": 21,
      "kind": "method",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     }
    ],
    "edges": []
   }
  }
 },
 {
  "name": "zune-2008",
  "title": "Microsoft Zune 30, December 31 2008: every device freezes on boot",
  "what_happened": "The real-time-clock driver turned days since 1980 into a year with a loop that subtracts 365 or 366 days per year. On the last day of a leap year (day 366 of 2008) neither branch changed anything: the loop spun forever and the players hung at boot until the battery ran down.",
  "sources": [
   "Freescale MC13783 RTC driver, ConvertDays() in rtc.c, as published after the incident; Microsoft Zune support statement (January 2009)",
   "CNN Money, \"Users of Zune MP3 players report glitch\" (December 31 2008)"
  ],
  "language": "Python port of the C driver.",
  "steps": [
   {
    "dir": "2-days-to-year",
    "what": "Convert the day count to a calendar year (the driver's ConvertDays).",
    "verdict": "block",
    "status": "caught",
    "rules": {
     "loop-without-progress": "caught"
    },
    "unwanted": [],
    "known_miss": "",
    "findings": [
     {
      "rule": "loop-without-progress",
      "severity": "high",
      "message": "this loop never ends when is_leap_year(year) and not days > 366 (days == 366): nothing on that path changes days or year, and nothing leaves the loop",
      "path": "zune/rtc.py",
      "line": 16,
      "detail": "Once it runs, the program hangs or keeps doing the same thing forever. Knight Capital, 2012: a loop like this sent orders without end ($460 million in 45 minutes). Zune, 2008: one like it froze every Zune 30 on the last day of a leap year.",
      "fix": "Make every way through the loop change what its condition reads, or leave the loop (break, return or raise) where it cannot."
     }
    ],
    "changes": [
     {
      "kind": "body",
      "name": "zune.clock.boot",
      "path": "zune/clock.py",
      "line": 4,
      "detail": "function body changed"
     },
     {
      "kind": "added",
      "name": "zune.rtc.year_from_days",
      "path": "zune/rtc.py",
      "line": 13,
      "detail": "new function"
     }
    ],
    "affected": []
   },
   {
    "dir": "3-fix-break-on-day-366",
    "what": "The fix: leave the loop on day 366 of a leap year.",
    "verdict": "ok",
    "status": "quiet",
    "rules": {},
    "unwanted": [],
    "known_miss": "",
    "findings": [],
    "changes": [
     {
      "kind": "body",
      "name": "zune.rtc.year_from_days",
      "path": "zune/rtc.py",
      "line": 13,
      "detail": "function body changed"
     }
    ],
    "affected": [
     {
      "name": "zune.clock.boot",
      "path": "zune/clock.py",
      "line": 4,
      "hops": 1,
      "score": 0.54,
      "why": "zune.clock.boot calls zune.rtc.year_from_days (zune/clock.py:5)"
     }
    ]
   }
  ],
  "date": "December 31, 2008",
  "damage": [
   {
    "value": 24,
    "prefix": "~",
    "suffix": " hours",
    "label": "frozen at boot until the date rolled over (Microsoft: let the battery run down, recharge after noon GMT on January 1)"
   },
   {
    "text": "Every",
    "label": "30 GB Zune started on December 31, 2008 froze at boot"
   }
  ],
  "catch": {
   "rule": "loop-without-progress",
   "path": "zune/rtc.py",
   "line": 16,
   "says": "On day 366 of a leap year, is_leap_year(year) is true but days > 366 is false: neither days nor year changes, so while days > 365 spins forever.",
   "fix": "Leave the loop on day 366 of a leap year (the fix, in the next step)."
  },
  "story": {
   "step": "2-days-to-year",
   "what": "Convert the day count to a calendar year (the driver's ConvertDays).",
   "verdict": "block",
   "rules": {
    "loop-without-progress": "caught"
   },
   "files": [
    {
     "path": "zune/rtc.py",
     "state": "changed",
     "lines": [
      [
       " ",
       1,
       "\"\"\"Real-time clock: the firmware counts days since January 1, 1980.\"\"\""
      ],
      [
       " ",
       2,
       ""
      ],
      [
       " ",
       3,
       "ORIGIN_YEAR = 1980"
      ],
      [
       " ",
       4,
       "SECONDS_PER_DAY = 86400"
      ],
      [
       " ",
       5,
       ""
      ],
      [
       " ",
       6,
       ""
      ],
      [
       " ",
       7,
       "def is_leap_year(year: int) -> bool:"
      ],
      [
       " ",
       8,
       "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)"
      ],
      [
       " ",
       9,
       ""
      ],
      [
       " ",
       10,
       ""
      ],
      [
       " ",
       11,
       "def days_since_origin(seconds: int) -> int:"
      ],
      [
       " ",
       12,
       "    return seconds // SECONDS_PER_DAY + 1"
      ],
      [
       "+",
       13,
       "def year_from_days(days: int) -> int:"
      ],
      [
       "+",
       14,
       "    \"\"\"Calendar year of the day ``days`` (1 = January 1, 1980).\"\"\""
      ],
      [
       "+",
       15,
       "    year = ORIGIN_YEAR"
      ],
      [
       "+",
       16,
       "    while days > 365:"
      ],
      [
       "+",
       17,
       "        if is_leap_year(year):"
      ],
      [
       "+",
       18,
       "            if days > 366:"
      ],
      [
       "+",
       19,
       "                days -= 366"
      ],
      [
       "+",
       20,
       "                year += 1"
      ],
      [
       "+",
       21,
       "        else:"
      ],
      [
       "+",
       22,
       "            days -= 365"
      ],
      [
       "+",
       23,
       "            year += 1"
      ],
      [
       "+",
       24,
       "    return year"
      ]
     ],
     "marks": [
      16
     ]
    }
   ],
   "changes": [
    {
     "kind": "body",
     "name": "zune.clock.boot",
     "path": "zune/clock.py",
     "line": 4,
     "detail": "function body changed"
    },
    {
     "kind": "added",
     "name": "zune.rtc.year_from_days",
     "path": "zune/rtc.py",
     "line": 13,
     "detail": "new function"
    }
   ],
   "findings": [
    {
     "rule": "loop-without-progress",
     "severity": "high",
     "message": "this loop never ends when is_leap_year(year) and not days > 366 (days == 366): nothing on that path changes days or year, and nothing leaves the loop",
     "path": "zune/rtc.py",
     "line": 16,
     "detail": "Once it runs, the program hangs or keeps doing the same thing forever. Knight Capital, 2012: a loop like this sent orders without end ($460 million in 45 minutes). Zune, 2008: one like it froze every Zune 30 on the last day of a leap year.",
     "fix": "Make every way through the loop change what its condition reads, or leave the loop (break, return or raise) where it cannot."
    }
   ],
   "affected": [],
   "map": {
    "nodes": [
     {
      "id": "zune.clock.boot",
      "label": "boot",
      "path": "zune/clock.py",
      "line": 4,
      "kind": "function",
      "change": "body",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "zune.rtc.ORIGIN_YEAR",
      "label": "ORIGIN_YEAR",
      "path": "zune/rtc.py",
      "line": 3,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "zune.rtc.SECONDS_PER_DAY",
      "label": "SECONDS_PER_DAY",
      "path": "zune/rtc.py",
      "line": 4,
      "kind": "constant",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "zune.rtc.days_since_origin",
      "label": "days_since_origin",
      "path": "zune/rtc.py",
      "line": 11,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "zune.rtc.is_leap_year",
      "label": "is_leap_year",
      "path": "zune/rtc.py",
      "line": 7,
      "kind": "function",
      "change": "",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": false
     },
     {
      "id": "zune.rtc.year_from_days",
      "label": "year_from_days",
      "path": "zune/rtc.py",
      "line": 13,
      "kind": "function",
      "change": "added",
      "removed": false,
      "score": 0,
      "hops": 0,
      "finding": true
     }
    ],
    "edges": [
     {
      "src": "zune.clock.boot",
      "dst": "zune.rtc.year_from_days",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "zune.clock.boot",
      "dst": "zune.rtc.days_since_origin",
      "kind": "calls",
      "guess": false
     },
     {
      "src": "zune.rtc.days_since_origin",
      "dst": "zune.rtc.SECONDS_PER_DAY",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "zune.rtc.year_from_days",
      "dst": "zune.rtc.ORIGIN_YEAR",
      "kind": "reads",
      "guess": false
     },
     {
      "src": "zune.rtc.year_from_days",
      "dst": "zune.rtc.is_leap_year",
      "kind": "calls",
      "guess": false
     }
    ]
   }
  }
 }
];
