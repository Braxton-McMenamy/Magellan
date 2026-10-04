# Fine apart, broken together

One person adds a required parameter to `parse_record` and fixes the call they know about.
Another, on their own branch, adds a new call the old way. Different files: git merges them
without a word, and each branch's checks pass. The merge fails in production.

**Share my work in progress** pushes your working tree to `refs/wip/<your name>` on your
remote: no commit, no branch, your index and stash untouched.

**Check against my team** combines your work with each teammate's shared work and shows only
the problems the combination has, on the line, saying whose work they come from.

Whoever can read the repository can read what you share: on a public repository, everyone.
