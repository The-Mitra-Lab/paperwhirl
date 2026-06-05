<p align="center">
  <img src="docs/logo.png" alt="" width="260">
</p>

<h1 align="center">PaperWhirl</h1>

<p align="center">
  <b>Fast, figure-grounded paper review for your Mac.</b><br>
  Drop in a paper, get a claim-driven summary with every figure explained,
  then talk to it.
</p>

<p align="center">
  <img src="docs/screenshots/hero.png" alt="A PaperWhirl summary with the Discuss panel open" width="840">
</p>

## Why PaperWhirl?

It's becoming more and more difficult to stay on top of all of the interesting
manuscripts in one's field. A good strategy to quickly extract the gestalt of a
manuscript is to read the abstract, print the figures, and have an interactive
conversation with your favorite AI. But this is still inefficient. One has to
type a prompt for every paper, print the figures for each discussion, make sure
the LLM can actually "see" the PDF properly, and keep track of old discussions.
PaperWhirl solves these problems.

## How Does PaperWhirl Work?

PaperWhirl extracts the text and figures from a manuscript, produces an overview
and a summary of each figure, and is ready to discuss it with you interactively.
Each figure can be unrolled individually, so you can focus on one section of the
paper at a time.

To get started, just drag and drop a PDF — or paste a PMID, PMC number, bioRxiv
link, DOI, or URL — into the bar. Hit the **D** button in the top-right corner to
discuss. When you're done, you can download the full manuscript, the AI summary,
or the summary plus your discussion as a PDF, then file the paper into a reading
list for easy retrieval later.

## FAQ

### What do I need to use PaperWhirl?

- **An Apple Silicon Mac** (M1 or later).
- **An OpenAI API key.** PaperWhirl uses your own key to read and summarize
  papers — see *How do I get and enter an API key?* below. Costs are pennies-scale
  per paper (see *What does it cost?*).
- **For paywalled papers, a network with publisher access.** Open-access papers
  and preprints work from anywhere. For subscription journals, PaperWhirl can
  only fetch what your network can — so you'll want to be on your institution's
  network or VPN.

### How do I install PaperWhirl?

1. Download **PaperWhirl.zip** from the [latest release](https://github.com/The-Mitra-Lab/paperwhirl/releases/latest).
2. Unzip it.
3. Drag **PaperWhirl.app** into your **Applications** folder.
4. Open it. The first time, macOS may ask you to confirm — click **Open**.

No terminal, no Python, no other setup required.

### How do I get and enter an API key?

PaperWhirl runs on your own OpenAI API key. There are two ways to get one:

- **Bring your own key (anyone).** Create a key at the
  [OpenAI Platform](https://platform.openai.com/api-keys) — see the steps below.
- **Mitra Lab members.** Ask Rob for an invite to the lab's OpenAI project, then
  create your key inside that project so usage is billed to the lab.

**Creating your key:**

1. Sign in to the [OpenAI Platform](https://platform.openai.com/api-keys).
2. In the left sidebar, click **API keys**.
3. Make sure you're in the correct organization/project.
4. Click **Create new secret key** and give it a clear name (e.g. *PaperWhirl*).
5. **Copy the key immediately** — OpenAI only shows it once. Paste it somewhere
   safe (a password manager or a local note).

**Entering it:** open PaperWhirl and paste the key into the API-key field when
prompted. You'll also pick a folder where your papers and reading lists are
stored. That's it — the key lives only on your Mac (see *Is my API key safe?*).

### How do I use PaperWhirl?

**1. Load a paper.** Drag and drop a PDF, or paste a PMID, PMC number, bioRxiv
link, DOI, or URL into the bar. You can also click the **S** button in the
top-right to search for papers, then click a result to load it.

<p align="center">
  <img src="docs/screenshots/search.png" alt="Search panel with results" width="360">
</p>

**2. Read the overview and figures.** In about 20–30 seconds you'll get the
overview and a summary of each figure; the full paper takes 1–2 minutes for the
LLM to finish analyzing. Figures start rolled up — unroll any of them to read
the summary and look at the figure.

<p align="center">
  <img src="docs/screenshots/summary.png" alt="Overview and figure summaries" width="760">
</p>

**3. Discuss it.** Click the **D** button in the top-right to open the discussion
panel. You can chat normally about anything in the paper — the text, the figures,
or the references; the LLM can see all of it. You don't have to wait for the full
analysis: discussion is ready as soon as the figure roll-bars appear.

<p align="center">
  <img src="docs/screenshots/discuss.png" alt="Discuss panel open next to a figure" width="760">
</p>

**4. Save it to a reading list.** Hit **Save to list** to file the paper for
later. Reading lists live in the left pane. Within a list you can sort papers by
title or by date added; papers are named by first author and year by default,
but you can rename them to anything.

<p align="center">
  <img src="docs/screenshots/reading-lists.png" alt="Reading lists in the left pane" width="240">
</p>

**5. Download what you need.** Three buttons let you download a PDF of the full
manuscript, the AI summary, or the summary plus your discussion.

<p align="center">
  <img src="docs/screenshots/downloads.png" alt="Download manuscript, summary, or summary plus discussion" width="620">
</p>

### What papers and sources work?

Paste any of these into the bar, or drag in a PDF:

- A **PMID**, **PMC number**, **bioRxiv** link, **DOI**, or article **URL**
- A **PDF** dragged straight from your computer

PaperWhirl pulls the cleanest available source automatically (PubMed Central,
publisher sites, preprint servers). Open-access papers and preprints work from
anywhere; paywalled papers depend on your network having publisher access
(see *What do I need to use PaperWhirl?*).

### Is my API key safe?

Yes. Your key is stored locally on your Mac and is used only to talk to OpenAI
directly from your machine. It never leaves your computer for anywhere else —
there's no PaperWhirl server, and we never see it.

### What does it cost?

PaperWhirl itself is free. You pay only for your own OpenAI usage, which is
pennies-scale per paper. Two ways to keep costs down:

- **Choose a cheaper model.** PaperWhirl defaults to a frontier model for the
  best summaries, but you can switch to a cheaper tier (e.g. a *mini* or *nano*
  model) in Settings for a fraction of the cost.
- **Set a spending limit.** You can cap monthly spend on your key directly in
  the [OpenAI Platform](https://platform.openai.com/settings/organization/limits),
  so there are no surprises.

---

## `models.json`

The live list of models the app offers in Settings. The app fetches this on
startup, so the in-app model menu can be updated **without shipping a new
build** — edit this file to change the menu for all installs. The app falls
back to a built-in list if this file is unreachable.
