# HubSpot App Marketplace Listing Requirements

To list your application on the official HubSpot App Marketplace, you must satisfy all technical, functional, and operational requirements. This document compiles the official criteria from the Setup Guide and App Listing Requirements pages.

## 1. Technical & Performance Requirements
- **OAuth 2.0 Integration**: Your app must use HubSpot's standard OAuth 2.0 flow for authentication.
- **Active Installs**: You must have a minimum of **3 active installs** of the app across different HubSpot portals.
- **API Reliability**: Your app must maintain at least a **95% success rate** for API calls (no consistent 4xx or 5xx errors).
- **Webhook Latency**: If your app uses Webhooks, it must respond with a `200 OK` status within **3 seconds**, or HubSpot will drop the webhook and record a failure.
- **No Test Scopes**: Your app must only request scopes that it actually utilizes in production.

## 2. Listing Setup & Marketing Assets
- **App Name & Tagline**: A clear app name and a brief tagline (max 140 characters).
- **App Logo**: High-quality PNG or JPG (minimum 400x400px, 1:1 aspect ratio).
- **Screenshots**: Between 3 to 10 high-resolution screenshots demonstrating the integration inside HubSpot (or your app).
- **Demo Video**: At least 1 YouTube or Vimeo link showing a user setting up the app and using its core features.
- **Detailed Description**: A comprehensive overview of what the app does, the features it provides, and how it benefits HubSpot users.

## 3. Support & Compliance Links
To protect HubSpot customers, your listing must provide publicly accessible documentation:
- **Setup Guide**: A dedicated URL (hosted on your site) containing step-by-step instructions with screenshots on how to install and configure the integration.
- **Support Contact**: An actively monitored support email address (e.g., support@yourdomain.com).
- **Terms of Service (ToS)**: A direct link to your app's Terms of Service.
- **Privacy Policy**: A direct link to your app's Privacy Policy, explicitly detailing how HubSpot customer data is handled, stored, and deleted.

## 4. The Setup Guide Requirements
Your Setup Guide (which you must link in the listing) must explicitly include:
1. **Prerequisites**: What HubSpot tiers or external licenses are required?
2. **Installation Steps**: Clear instructions on how to initiate the OAuth flow.
3. **Configuration**: How to map users, set up data syncing, or select features.
4. **Usage Instructions**: A brief walkthrough of the daily workflow using the app.
5. **Uninstallation**: Instructions on how to disconnect the app and remove data.

## Next Steps
Once you have collected these assets and verified your technical metrics, you can submit the app for review directly from your HubSpot Developer Portal under **App Marketplace > Listings**. The review process typically takes 3-5 business days.
