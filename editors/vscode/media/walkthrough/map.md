# The map: where the change goes

The dangerous change is rarely the one you're looking at. When a function changes, the map
follows it to everything that calls it, then to their callers, hop by hop, with a score for
how likely each one is to feel it.

- **Orange**: what you changed.
- **Red, by heat**: what the change reaches, and how hard.
- **A red ring**: a finding is there.

It reads Python, Java, C, Fortran and COBOL, and follows calls between them: a C function
behind a Java `native` method reaches its Java callers.

**Global** is the whole project in 3D. Click into a function in the editor and **Local** shows
it and its neighbourhood, hop by hop, following the cursor; the padlock (or **L**) holds it
still. Click a dot with a red ring for its findings; click any other dot (double-click, in 3D)
to open its line.
