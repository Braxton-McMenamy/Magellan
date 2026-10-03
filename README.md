# My Site

A plain HTML/CSS starter. No build step, no dependencies.

## Run locally

Open `index.html` in a browser, or serve the folder:

    python3 -m http.server 8000

Then visit http://localhost:8000.

## Push to Git

    git init
    git add .
    git commit -m "Initial commit"
    git branch -M main
    git remote add origin <your-repo-url>
    git push -u origin main

## Self-host

Copy the files to any web server's document root, for example:

    # nginx: /var/www/my-site  (point `root` at it)
    # Apache: /var/www/html
    # or: rsync -av ./ user@server:/var/www/my-site/

You can also host it free on GitHub Pages: repo Settings > Pages > deploy from the `main` branch.
