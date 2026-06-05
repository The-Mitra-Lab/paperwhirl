# PaperWhirl

A fast, figure-grounded research-paper review app for the Mitra Lab. Paste a
paper (DOI / PMID / arXiv ID / URL) or search, and get a claim-driven summary
grounded in the paper's figures, with an interactive Discuss panel.

## Install (macOS)

Download the latest `PaperWhirl.zip` from [Releases](../../releases), unzip,
drag **PaperWhirl.app** into Applications, and open it. It's signed and
notarized, so it launches with no Gatekeeper warning — no terminal, Python,
or other prerequisites required.

## API key

PaperWhirl uses your own OpenAI API key (entered on first launch or in
Settings; stored locally on your Mac, never sent anywhere else). Lab members:
ask Rob for a lab-project key. Others: use your own OpenAI key.

## `models.json`

The live list of models the app offers in Settings. The app fetches this on
startup, so the in-app model menu can be updated **without shipping a new
build** — edit this file to change the menu for all installs. The app falls
back to a built-in list if this file is unreachable.
