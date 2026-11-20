# Egress: presentation slides

A 10-slide deck that follows `PITCH.md` (about 2:30 plus the live demo). Each slide gives the
on-slide text (keep it this short), the visual and the speaker notes. At the end there is a
prompt ready to paste into Gamma, Canva Magic Design, PowerPoint Copilot or Google Slides with
Gemini.

---

## Design rules (from the brand kit)

| | |
|---|---|
| Format | 16:9, 1920 × 1080 |
| Colours | Black `#000000` title slides; light `#F5F5F5` with white cards for content; **one** accent `#0099FF` (blue text on light backgrounds: `#0070C9`) |
| Before / after | AWS side in warm orange outlines `#D97757`; open-source side in blue `#0099FF` |
| Score colours | Medium `#9A5B00`, low `#18794E`, high `#C13B40` |
| Font | Inter, headings 800–900 with tight letter-spacing; labels small, UPPERCASE, wide spacing |
| Shapes | Rounded cards (30 px), pill-shaped tags; flat, with no gradients or shadows |
| Logo | White "E" on a blue rounded square + "Egress" wordmark, top-left |
| Avoid | AWS logos or icons (use text labels), stock photos, clip-art clouds, padlocks or flags |
| Text | At most one headline and three short lines per slide; numbers big |

---

## Slide 1: Title  ·  *black*

- **Headline:** Leave AWS.
- **Second line (blue):** Keep your app.
- **Sub:** Measure cloud lock-in. Generate the exit. Run it in minutes.
- **Footer pills:** Open source · Apache 2.0 · EU-sovereign stack
- **Visual:** logo top-left, lots of empty black space.
- **Notes:** "Moving into the cloud is easy. Leaving it is not."

## Slide 2: The problem  ·  *light*

- **Kicker:** THE PROBLEM
- **Headline:** Every proprietary API is a contract you can't easily cancel.
- **Three cards:**
  - **Hidden cost**: code and architecture tied to S3, SQS, Lambda, IAM.
  - **No visibility**: nobody can measure how locked in they are.
  - **New pressure**: data sovereignty and the EU Data Act require that switching is possible.
- **Notes:** prices, customers asking where data lives, regulation. *"You only discover your lock-in when you try to leave."*

## Slide 3: The solution  ·  *black*

- **Headline:** Egress
- **Sub:** From a feeling, to a number, to a working exit.
- **Visual:** three large numbered steps in a row: **01 · Scan → 02 · Migrate → 03 · Run**,
  with the arrows in blue.
- **Notes:** "Egress does three things."

## Slide 4: 01 · Scan  ·  *light*

- **Kicker:** 01 · SCAN
- **Headline:** Find the lock-in.
- **Big number:** **63**/100 in `#9A5B00`, labelled *medium lock-in*
- **Facts:** 32 findings · 7 AWS services · Terraform, compose, Python, requirements
- **Small print:** Score = 50% proprietary API + 30% data format + 20% egress exposure
- **Visual:** screenshot of the score card and service chips (Lambda, S3, SQS, CloudWatch,
  IAM, RDS, boto3).
- **Notes:** explainable score; every finding points to a file and line; SARIF for CI.

## Slide 5: 02 · Migrate  ·  *light*

- **Kicker:** 02 · MIGRATE
- **Headline:** Generate the exit plan.
- **Mapping table** (AWS in orange, open source in blue):
  - S3 → SeaweedFS
  - RDS → PostgreSQL 16
  - SQS → RabbitMQ
  - Lambda → container worker
  - IAM → generated credentials
- **Callout:** LLM (Mistral, EU) writes the code in **~30 s**. Egress verifies every line.
- **Notes:** complete files, exact diffs computed by Egress, then compile + config + re-scan.

## Slide 6: Before → after  ·  *light*

- **Headline:** Same app. Zero AWS.
- **Visual:** the before/after card, **63 → 0**, with a green pill "−63 pts · 100% less lock-in".
  Below it, both architecture diagrams: orange AWS boxes on top, blue open-source boxes below.
- **Notes:** "The score goes from 63 to zero."

## Slide 7: 03 · Run (live demo)  ·  *black*

- **Kicker:** 03 · RUN
- **Headline:** One click. Five containers. Live.
- **List:** postgres · s3 · rabbitmq · api · worker → **healthy**
- **Line:** Upload → storage → database → queue → worker: `processed` ✓
- **Visual:** screenshot of the healthy container table and the smoke-test result.
  **Switch to the live app here.**
- **Notes:** Deploy, then Smoke test. If something fails: "this is exactly why the fallback exists".

## Slide 8: Trust by design  ·  *light*

- **Kicker:** WHY YOU CAN TRUST IT
- **Headline:** The AI proposes. Egress verifies.
- **Four cards:**
  - **Security policy**: pinned image digests, no privileged containers, ports on localhost only
  - **Privacy**: secrets redacted, you choose what is sent, offline and local-model modes
  - **Verification**: compile, contract and re-scan checks, plus one automatic repair round
  - **Runtime fallback**: if the AI code crashes, Egress shows the logs and redeploys a verified app
- **Notes:** mention the real Flask 3 bug caught in testing.

## Slide 9: What's next  ·  *light*

- **Headline:** From one repo to every repo.
- **Roadmap pills:** Azure & GCP · Node & Java · Kubernetes/Helm output · Cost comparison ·
  GitHub Action with the lock-in score on every PR
- **Who it's for:** EU scale-ups, regulated industries and the public sector, cloud-exit consultancies
- **Notes:** open-source core; business model as in `PITCH.md` (adjust to your plan).

## Slide 10: Close  ·  *black*

- **Headline:** Leave AWS.
- **Second line (blue):** Keep your app.
- **Sub:** Egress · open source (Apache 2.0) · *repository URL / QR code*
- **Small:** 160+ automated tests · works offline · EU LLM ready
- **Notes:** pause, then "Thank you, happy to take questions."

---

## Prompt for an AI slide tool

```text
Create a 10-slide, 16:9 pitch deck in English for "Egress", an open-source developer tool that
scans a code repository for AWS lock-in, scores it 0-100, generates the migration to open-source,
EU-sovereign services with an LLM, verifies it, and runs the new stack with docker compose.
Tagline: "Leave AWS. Keep your app."

Design: monochrome with ONE accent colour #0099FF (use #0070C9 for blue text on light
backgrounds). Title and section slides on black #000000 with white text; content slides on light
grey #F5F5F5 with white rounded cards (30 px radius) and pill-shaped tags. Font Inter: headings
extra-bold with tight letter-spacing, small uppercase labels with wide spacing. Flat design: no
gradients, shadows, stock photos or AWS logos (use plain text labels). AWS "before" elements use
orange outlines #D97757, open-source "after" elements use blue outlines #0099FF. Big numbers, at
most 3 short lines per slide.

Slides:
1. Title (black): "Leave AWS." / "Keep your app." in blue. Sub: "Measure cloud lock-in. Generate
   the exit. Run it in minutes." Pills: Open source · Apache 2.0 · EU-sovereign stack.
2. Problem: "Every proprietary API is a contract you can't easily cancel." Three cards: Hidden
   cost (code tied to S3, SQS, Lambda, IAM); No visibility (nobody measures lock-in); New pressure
   (data sovereignty, EU Data Act).
3. Solution (black): "Egress" and three steps "01 · Scan → 02 · Migrate → 03 · Run".
4. 01 · Scan: "Find the lock-in." Big score 63/100 (colour #9A5B00, "medium lock-in"); 32 findings,
   7 AWS services; Terraform, compose, Python, requirements. Footnote: Score = 50% proprietary API
   + 30% data format + 20% egress exposure. Leave space for a product screenshot.
5. 02 · Migrate: "Generate the exit plan." Mapping S3→SeaweedFS, RDS→PostgreSQL 16, SQS→RabbitMQ,
   Lambda→container worker, IAM→generated credentials. Callout: "LLM (Mistral, EU) writes the code
   in ~30 s. Egress verifies every line."
6. Before → after: "Same app. Zero AWS." 63 → 0 with a green pill "−63 pts · 100% less lock-in"
   (#18794E); space for before/after architecture diagrams.
7. 03 · Run (black): "One click. Five containers. Live." postgres · s3 · rabbitmq · api · worker →
   healthy; "Upload → storage → database → queue → worker: processed ✓". Space for a screenshot.
8. Trust: "The AI proposes. Egress verifies." Four cards: Security policy; Privacy; Verification;
   Runtime fallback.
9. Next: "From one repo to every repo." Roadmap pills: Azure & GCP, Node & Java, Kubernetes/Helm,
   cost comparison, GitHub Action. Audience: EU scale-ups, regulated industries, public sector.
10. Close (black): "Leave AWS." / "Keep your app." in blue; "Egress · open source (Apache 2.0)";
   space for a QR code to the repository.
Add short speaker notes to each slide.
```

---

## Screenshots to add (from the real app)

| Slide | Screenshot |
|---|---|
| 4 | Score card (63/100) + "Services detected" chips |
| 5 | *Mapping* tab or the *Code diffs* tab showing `worker.py` |
| 6 | Before/after card and the two architecture diagrams |
| 7 | Container status table (5 healthy) + smoke-test result |

Take them at 1920 px width with the browser zoom at 125%, on `sample_app`. Hide the address bar
or make sure it never shows the `?key=` presenter URL.
