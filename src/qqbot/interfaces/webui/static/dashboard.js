/* Dashboard endpoint vocabulary is kept in a separate asset for integrations.
 * The existing app.js owns rendering so the original Kinna console shell stays intact.
 */
const DASHBOARD_ENDPOINTS = ["/api/dashboard/overview", "/api/dashboard/traces", "/api/dashboard/wiki"];
function dashboardText(value) { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node; }

const DASHBOARD_LABELS = ["运行总览", "核心配置"];
