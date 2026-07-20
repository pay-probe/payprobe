/** Two-level navigation: top-level groups -> child "tools". */
export interface NavChild {
  label: string;
  route: string;
  icon?: string; // pp-icon name (function icon for the tile)
  badge?: string;
  adminOnly?: boolean; // hidden unless the signed-in user has the admin role
}
export interface NavItem {
  label: string;
  icon: string; // pp-icon name
  route?: string; // present => leaf item (no children)
  children?: NavChild[]; // present => expandable group
  adminOnly?: boolean;
  /** Accent color for this section (icon tint + active state). Falls back to --brand. */
  accent?: string;
}

export const NAV: NavItem[] = [
  {
    label: "Dashboard",
    icon: "dashboard",
    route: "/dashboard",
    accent: "#0ea5e9",
  },
  {
    label: "Scenarios",
    icon: "flow",
    route: "/constructor",
    accent: "#8b5cf6",
  },
  // ADR-0007: poke before you author — ad-hoc execution against anything
  // addressable (connections × envs, simulators, participants, functions).
  {
    label: "Playground",
    icon: "beaker",
    route: "/playground",
    accent: "#22c55e",
  },
  {
    label: "Execution",
    icon: "activity",
    accent: "#10b981",
    children: [
      { label: "Start run", route: "/run-monitor", icon: "play" },
      { label: "Run history", route: "/runs", icon: "clock" },
      { label: "Trends", route: "/trends", icon: "chart" },
      { label: "Model Studio", route: "/model-studio", icon: "sparkles" },
      { label: "Failure Taxonomy", route: "/insight-categories", icon: "list" },
      { label: "Schedules", route: "/schedules", icon: "calendar" },
    ],
  },
  {
    label: "Simulated Network",
    icon: "flow",
    accent: "#06b6d4",
    children: [
      // ATLAS §12: three distinct jobs, clear names — a control board, a live
      // diagram, and replay (not "Topology Map 1/2/3").
      { label: "Network Control", route: "/topology-map", icon: "server" },
      { label: "Live Network Map", route: "/network-map", icon: "network" },
      { label: "Network Replay", route: "/topology-map-3", icon: "network" },
      { label: "Networks", route: "/network-flows", icon: "flow" },
      { label: "Participant Flows", route: "/participants", icon: "flow" },
      {
        label: "Participant Groups",
        route: "/participant-groups",
        icon: "group",
      },
      { label: "Simulators", route: "/simulators", icon: "server" },
      { label: "NATS Cluster", route: "/nats", icon: "activity" },
      { label: "Live Sessions", route: "/peers", icon: "activity" },
    ],
  },
  {
    label: "Load Testing",
    icon: "flash",
    accent: "#f59e0b",
    children: [
      { label: "Load Test", route: "/load", icon: "zap" },
      { label: "Resilience", route: "/resilience", icon: "shield" },
      { label: "Load Workers", route: "/load-workers", icon: "server" },
    ],
  },
  {
    label: "Configure",
    icon: "settings",
    accent: "#ec4899",
    children: [
      { label: "Connections", route: "/connections", icon: "plug" },
      { label: "Environments", route: "/environments", icon: "box" },
      { label: "Adapters", route: "/adapters", icon: "cpu" },
      { label: "Starter Flows", route: "/starter-flows", icon: "sparkles" },
      { label: "Message Formats", route: "/message-formats", icon: "format" },
      { label: "Variables", route: "/variables", icon: "variable" },
      { label: "Test Data", route: "/test-data", icon: "database" },
      { label: "Tables", route: "/tables", icon: "table" },
      { label: "Secrets", route: "/secrets", icon: "lock" },
    ],
  },
  {
    label: "Reference",
    icon: "book",
    accent: "#f97316",
    children: [
      { label: "Step Types", route: "/step-types", icon: "list" },
      { label: "Test-case Packs", route: "/packs", icon: "package" },
      { label: "ISO 8583 Inspector", route: "/inspector", icon: "search" },
      { label: "Docs", route: "/docs", icon: "book" },
    ],
  },
  {
    label: "Diagnostics",
    icon: "activity",
    accent: "#ef4444",
    children: [
      { label: "Network Trace", route: "/network-trace", icon: "activity" },
      { label: "Platform Diagnostics", route: "/diagnostics", icon: "pulse" },
    ],
  },
  {
    label: "Integrations",
    icon: "activity",
    route: "/integrations",
    accent: "#14b8a6",
  },
  {
    label: "Settings",
    icon: "settings",
    route: "/settings",
    accent: "#64748b",
  },
  {
    label: "Users & Roles",
    icon: "shield",
    route: "/users",
    adminOnly: true,
    accent: "#d946ef",
  },
];
