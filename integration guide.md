# REHA Connect v2: Integration Testing Guide

Ensure every layer of your hardened Slack-HubSpot Identity Bridge is functioning perfectly with this end-to-end verification suite.

## 📥 1. Onboarding & Handshake (The "Success-First" Flow)

Verify the refined installation experience that transitions smoothly between Slack and HubSpot.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **1.1** | **Clean Start** | Uninstall the bot from Slack and reset the portal in HubSpot (if possible). |
| **1.2** | **Slack Install** | Click 'Add to Slack'. The browser should redirect to HubSpot **WITHOUT** the bot sending any proactive messages yet. |
| **1.3** | **HubSpot Auth** | Complete the HubSpot OAuth. You should be redirected back to the Slack App Home. |
| **1.4** | **Success DM** | Verify a rich DM arrives from the bot confirming the success. |
| **1.5** | **Tip Review** | Confirm the DM contains: *"📌 Tip: Please remember to invite the bot (`@REHA Connect`) to your channels."* |

## 🧠 2. AI & Sentiment Intelligence

Verify that the local "Sovereign Sentiment Engine" is operating correctly without blocking the server.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **2.1** | **Warmup Check** | Check server logs ~10s after startup. Verify: *"Initializing Sovereign Sentiment Engine"* appears automatically. |
| **2.2** | **AI Analysis** | Share a HubSpot link in Slack with a positive or negative note attached. |
| **2.3** | **Score Insight** | Verify the "AI Insights" card reflects a **Sentiment Score** based on the note's tone. |
| **2.4** | **Non-Blocking** | Ensure you can still run `/hs search` while the AI engine is processing. |

## 🛡️ 3. Infrastructure & Resilience (Render Optimization)

Verify that the bot remains responsive and quiet in production environments.

> [!IMPORTANT]
> **Log Silence**: During these tests, your console logs should NO LONGER show "Identity Pivot" or "Handshake Successful" at the standard INFO level. These are now silently cached for performance.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **3.1** | **Cold Boot** | Restart the server. Verify "Startup complete" appears instantly (within 1-2 seconds). |
| **3.2** | **App Home** | Open the App Home tab in Slack. Verify it renders your HubSpot status without `invalid_auth` errors. |
| **3.3** | **Burst Testing** | Send 5 rapid `/hs search` commands. Check logs to ensure NO repetitive identity handshakes were triggered. |

## 💡 4. Shared Channel Integration

Verify the core collaboration features in group channels.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **4.1** | **Invite Bot** | Go to a public channel and type `/invite @REHA Connect`. |
| **4.2** | **Search Test** | Type `/reha search [contact_name]`. Verify the bot returns results correctly in the channel. |
| **4.3** | **Identity God-View** | If you have multiple HubSpot portals, verify the bot correctly surfaces data from the *primary* linked portal for that Slack team. |

## 🎫 5. Help Desk Continuity

Verify the 1:1 threading sync between HubSpot and Slack.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **5.1** | **New Ticket** | Create a new ticket in HubSpot assigned to your email. |
| **5.2** | **Thread Creation** | Verify a new message arrives in your designated Slack ticket channel. |
| **5.3** | **Slack Reply** | Reply to the Slack thread. Verify it appears as an **Internal Note** on the HubSpot ticket. |
| **5.4** | **HubSpot Reply** | Reply from HubSpot. Verify it syncs back as a new message in the same Slack thread. |

## 💼 6. Advanced Deal Execution

Verify the new Deal action buttons work directly from Slack.

| Step | Action | Expected Result |
| :--- | :--- | :--- |
| **6.1** | **Load Deal** | Share a Deal link or use `/hs search` to render a Deal card. |
| **6.2** | **Change Close Date** | Click **Change Close Date** and select a new date. Verify the date updates in HubSpot. |
| **6.3** | **Log Next Step** | Click **Log Next Step**, enter text, and submit. Verify it syncs to the `hs_next_step` property. |
| **6.4** | **Pricing Calculator** | Click **Calculator**. Enter Quantity, Price, and Discount. Verify it calculates correctly and updates the Amount. |
| **6.5** | **Reassign Owner** | Click **Reassign Owner** and select a new user. Verify the ownership changes in HubSpot. |
