# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from html import escape

from fastapi.responses import HTMLResponse


def render_success_page(
    title: str,
    message: str,
    workspace_id: str,
    open_in_slack_url: str | None = None,
    primary_color: str = "#0d9488",  # REHA Teal
    secondary_color: str = "#4ade80",  # REHA Light Green
) -> HTMLResponse:
    """Renders a premium, branded success page for OAuth completion."""
    # Shorten ID for display
    display_id = workspace_id[:8].upper()

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{title}</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap"
              rel="stylesheet">
        <style>
            :root {{
                --primary: {primary_color};
                --secondary: {secondary_color};
                --bg: #0f172a;
                --card-bg: rgba(30, 41, 59, 0.7);
                --text: #f8fafc;
                --text-muted: #94a3b8;
            }}

            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}

            body {{
                font-family: 'Outfit', sans-serif;
                background-color: var(--bg);
                background-image:
                    radial-gradient(circle at 20% 20%, rgba(13, 148, 136, 0.1) 0%,
                                    transparent 40%),
                    radial-gradient(circle at 80% 80%, rgba(74, 222, 128, 0.1) 0%,
                                    transparent 40%);
                color: var(--text);
                height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }}

            .card {{
                background: var(--card-bg);
                backdrop-filter: blur(12px);
                border: 1px solid rgba(255, 255, 255, 0.1);
                padding: 3rem;
                border-radius: 24px;
                width: 100%;
                max-width: 480px;
                text-align: center;
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1);
            }}

            @keyframes slideUp {{
                from {{ opacity: 0; transform: translateY(20px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}

            .icon-wrapper {{
                width: 80px;
                height: 80px;
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                border-radius: 20px;
                margin: 0 auto 2rem;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 10px 20px -5px var(--primary);
            }}

            .check-icon {{
                color: white;
                font-size: 40px;
            }}

            h1 {{
                font-size: 2rem;
                font-weight: 600;
                margin-bottom: 1rem;
                letter-spacing: -0.02em;
            }}

            p {{
                color: var(--text-muted);
                line-height: 1.6;
                margin-bottom: 2.5rem;
            }}

            .reference-container {{
                background: rgba(255, 255, 255, 0.05);
                border: 1px dashed rgba(255, 255, 255, 0.1);
                padding: 1.5rem;
                border-radius: 16px;
                margin-bottom: 2rem;
            }}

            .label {{
                display: block;
                font-size: 0.75rem;
                text-transform: uppercase;
                letter-spacing: 0.1em;
                color: var(--text-muted);
                margin-bottom: 0.5rem;
            }}

            .workspace-id {{
                font-family: monospace;
                font-size: 1.5rem;
                color: var(--primary);
                font-weight: 600;
                letter-spacing: 0.2em;
            }}

            .btn-close {{
                background: var(--primary);
                color: white;
                border: none;
                padding: 0.75rem 2rem;
                border-radius: 12px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
            }}

            .btn-close:hover {{transform: translateY(-2px);
                box-shadow: 0 10px 20px -5px var(--primary);
            }}

            .btn-slack {{background: #4A154B; /* Slack Purple */
                color: white;
                border: none;
                padding: 1rem 2.5rem;
                border-radius: 12px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                text-decoration: none;
                display: block;
                margin-bottom: 1rem;
                font-size: 1.1rem;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }}

            .btn-slack:hover {{transform: translateY(-2px);
                background: #611f69;
                box-shadow: 0 10px 20px -5px #4A154B;
            }}

            .btn-copy {{
                background: none;
                border: 1px solid rgba(255, 255, 255, 0.1);
                color: var(--text-muted);
                padding: 0.5rem;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.2s;
                display: flex;
                align-items: center;
                justify-content: center;
            }}

            .btn-copy:hover {{
                background: rgba(255, 255, 255, 0.05);
                color: var(--primary);
                border-color: var(--primary);
            }}

            .btn-copy svg {{
                pointer-events: none;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon-wrapper">
                <span class="check-icon">✓</span>
            </div>
            <h1>{escape(title)}</h1>
            <p>{escape(message)}</p>

            <div class="reference-container">
                <span class="label">Reference Code</span>
                <div style="display: flex; align-items: center;
                            justify-content: center; gap: 1rem;">
                    <span class="workspace-id">{escape(display_id)}</span>
                    <button class="btn-copy" title="Copy Full ID"
                            onclick="copyToClipboard('{escape(workspace_id)}')">
                        <svg width="20" height="20" viewBox="0 0 24 24"
                             fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="9" y="9" width="13"
                                  height="13" rx="2" ry="2"></rect>
                            <path
d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1">
                            </path>
                        </svg>
                    </button>
                </div>
            </div>


            {
        f'<a href="{open_in_slack_url}" class="btn-slack" id="slack-deep-link">🚀 Open in Slack</a>'
        if open_in_slack_url
        else ""
    }

            <a href="https://rehaapps.com" class="btn-close"
               style="text-decoration: none; display: inline-block;">
                Go to Home
            </a>

            <script>
                function copyToClipboard(fullId) {{
                    navigator.clipboard.writeText(fullId).then(() => {{
                        const btn = document.querySelector('.btn-copy');
                        const originalHtml = btn.innerHTML;
                        btn.innerHTML = '✓';
                        btn.style.color = '#4ade80';
                        setTimeout(() => {{
                            btn.innerHTML = originalHtml;
                            btn.style.color = 'inherit';
                        }}, 2000);
                    }});
                }}
            </script>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


def render_error_page(
    title: str,
    message: str,
    primary_color: str = "#ef4444",  # Red
) -> HTMLResponse:
    """Renders a branded error page for OAuth failures."""
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{title}</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap"
              rel="stylesheet">
        <style>
            :root {{
                --primary: {primary_color};
                --reha-brand: #00D4AA;
                --bg: #0f172a;
                --card-bg: rgba(30, 41, 59, 0.7);
                --text: #f8fafc;
                --text-muted: #94a3b8;
            }}

            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}

            body {{
                font-family: 'Outfit', sans-serif;
                background-color: var(--bg);
                background-image:
                    radial-gradient(circle at 50% 50%, rgba(239, 68, 68, 0.1) 0%, transparent 40%);
                color: var(--text);
                height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }}

            .card {{
                background: var(--card-bg);
                backdrop-filter: blur(12px);
                border: 1px solid rgba(255, 255, 255, 0.1);
                padding: 3rem;
                border-radius: 24px;
                width: 100%;
                max-width: 480px;
                text-align: center;
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1);
            }}

            @keyframes slideUp {{
                from {{ opacity: 0; transform: translateY(20px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}

            .icon-wrapper {{
                width: 80px;
                height: 80px;
                background: linear-gradient(135deg, var(--primary), #b91c1c);
                border-radius: 20px;
                margin: 0 auto 2rem;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 10px 20px -5px var(--primary);
            }}

            .error-icon {{
                color: white;
                font-size: 40px;
                font-weight: bold;
            }}

            h1 {{
                font-size: 2rem;
                font-weight: 600;
                margin-bottom: 1rem;
                letter-spacing: -0.02em;
            }}

            p {{
                color: var(--text-muted);
                line-height: 1.6;
                margin-bottom: 2.5rem;
            }}

            .btn-close {{
                background: var(--reha-brand);
                color: #0f172a;
                border: none;
                padding: 0.75rem 2rem;
                border-radius: 12px;
                font-weight: 700;
                cursor: pointer;
                transition: all 0.2s;
                text-decoration: none;
                display: inline-block;
            }}

            .btn-close:hover {{
                transform: translateY(-2px);
                box-shadow: 0 10px 20px -5px var(--reha-brand);
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon-wrapper">
                <span class="error-icon">!</span>
            </div>
            <h1>{escape(title)}</h1>
            <p>{escape(message)}</p>

            <a href="https://rehaapps.com" class="btn-close">
                Go to Home
            </a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

