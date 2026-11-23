// Set this to your deployed Agent API base URL (no trailing slash) before deploying the site.
window.SHOPBOT_API = window.SHOPBOT_API || (location.hostname === "localhost" || location.hostname === "127.0.0.1"
  ? "http://localhost:8000"
  : "https://YOUR-AGENT-API.example.com");
