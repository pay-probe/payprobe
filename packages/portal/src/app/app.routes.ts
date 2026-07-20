import { Routes } from "@angular/router";

import { adminGuard } from "./auth/admin.guard";
import { authGuard } from "./auth/auth.guard";
import { pendingChangesGuard } from "./constructor/pending-changes.guard";

export const routes: Routes = [
  {
    path: "login",
    loadComponent: () =>
      import("./auth/login.component").then((m) => m.LoginComponent),
  },
  // Everything below requires a session. The component-less parent applies the
  // auth guard to all children (the shell + outlet live in AppComponent).
  {
    path: "",
    canActivateChild: [authGuard],
    children: [
      {
        path: "",
        redirectTo: "/dashboard",
        pathMatch: "full",
      },
      {
        path: "dashboard",
        data: { title: "Dashboard" },
        loadComponent: () =>
          import("./dashboard/dashboard.component").then(
            (m) => m.DashboardComponent,
          ),
      },
      {
        path: "run-monitor",
        data: { title: "Start run" },
        loadComponent: () =>
          import("./run-monitor/run-monitor.component").then(
            (m) => m.RunMonitorComponent,
          ),
      },
      {
        path: "run-monitor/:id",
        data: { title: "Run monitor" },
        loadComponent: () =>
          import("./run-monitor/run-monitor.component").then(
            (m) => m.RunMonitorComponent,
          ),
      },
      {
        path: "runs",
        data: { title: "Run history" },
        loadComponent: () =>
          import("./run-monitor/run-list.component").then(
            (m) => m.RunListComponent,
          ),
      },
      {
        // dashboard "Reports" link lands on the run registry
        path: "reports",
        data: { title: "Run history" },
        loadComponent: () =>
          import("./run-monitor/run-list.component").then(
            (m) => m.RunListComponent,
          ),
      },
      {
        path: "reports/:id",
        data: { title: "Run report" },
        loadComponent: () =>
          import("./run-monitor/run-report.component").then(
            (m) => m.RunReportComponent,
          ),
      },
      {
        path: "reports/:id/signoff",
        data: { title: "Go/No-Go sign-off" },
        loadComponent: () =>
          import("./run-monitor/run-signoff.component").then(
            (m) => m.RunSignoffComponent,
          ),
      },
      {
        path: "constructor",
        data: { title: "Scenarios" },
        loadComponent: () =>
          import("./constructor/scenario-list.component").then(
            (m) => m.ScenarioListComponent,
          ),
      },
      {
        path: "constructor/new",
        loadComponent: () =>
          import("./constructor/editor/constructor.component").then(
            (m) => m.ConstructorComponent,
          ),
        canDeactivate: [pendingChangesGuard],
      },
      {
        path: "constructor/:id",
        loadComponent: () =>
          import("./constructor/editor/constructor.component").then(
            (m) => m.ConstructorComponent,
          ),
        canDeactivate: [pendingChangesGuard],
      },
      {
        path: "step-types",
        data: { title: "Step Types" },
        loadComponent: () =>
          import("./constructor/catalog-editor.component").then(
            (m) => m.CatalogEditorComponent,
          ),
      },
      {
        path: "message-formats",
        data: { title: "Message Formats" },
        loadComponent: () =>
          import("./constructor/message-formats.component").then(
            (m) => m.MessageFormatsComponent,
          ),
      },
      {
        path: "variables",
        data: { title: "Variables" },
        loadComponent: () =>
          import("./constructor/variables.component").then(
            (m) => m.VariablesComponent,
          ),
      },
      {
        path: "tables",
        data: { title: "Tables" },
        loadComponent: () =>
          import("./constructor/tables.component").then(
            (m) => m.TablesComponent,
          ),
      },
      {
        path: "adapters",
        data: { title: "Adapters" },
        loadComponent: () =>
          import("./adapters/adapters.component").then(
            (m) => m.AdaptersComponent,
          ),
      },
      {
        path: "adapters/:key",
        data: { title: "Adapter" },
        loadComponent: () =>
          import("./adapters/adapter-detail.component").then(
            (m) => m.AdapterDetailComponent,
          ),
      },
      {
        path: "connections",
        data: { title: "Connections" },
        loadComponent: () =>
          import("./connections/connections.component").then(
            (m) => m.ConnectionsComponent,
          ),
      },
      {
        path: "insight-categories",
        data: { title: "Failure Taxonomy" },
        loadComponent: () =>
          import("./insights/taxonomy.component").then(
            (m) => m.InsightTaxonomyComponent,
          ),
      },
      {
        path: "model-studio",
        data: { title: "Model Studio" },
        loadComponent: () =>
          import("./insights/model-studio.component").then(
            (m) => m.ModelStudioComponent,
          ),
      },
      {
        path: "model-studio/new",
        data: { title: "New Model" },
        loadComponent: () =>
          import("./insights/model-wizard.component").then(
            (m) => m.ModelWizardComponent,
          ),
      },
      {
        path: "test-data",
        data: { title: "Test Data" },
        loadComponent: () =>
          import("./test-data/test-data.component").then(
            (m) => m.TestDataComponent,
          ),
      },
      {
        path: "secrets",
        data: { title: "Secrets" },
        loadComponent: () =>
          import("./secrets/secrets.component").then((m) => m.SecretsComponent),
      },
      {
        path: "diagnostics",
        data: { title: "Diagnostics" },
        loadComponent: () =>
          import("./diagnostics/diagnostics.component").then(
            (m) => m.DiagnosticsComponent,
          ),
      },
      {
        path: "environments",
        data: { title: "Environments" },
        loadComponent: () =>
          import("./environments/environments.component").then(
            (m) => m.EnvironmentsComponent,
          ),
      },
      {
        path: "users",
        data: { title: "Users & Roles" },
        canActivate: [adminGuard],
        loadComponent: () =>
          import("./users/users.component").then((m) => m.UsersComponent),
      },
      {
        path: "starter-flows",
        data: { title: "Starter Flows" },
        loadComponent: () =>
          import("./starter-flows/starter-flows.component").then(
            (m) => m.StarterFlowsComponent,
          ),
      },
      {
        path: "inspector",
        data: { title: "ISO 8583 Inspector" },
        loadComponent: () =>
          import("./inspector/inspector.component").then(
            (m) => m.InspectorComponent,
          ),
      },
      {
        path: "docs",
        data: { title: "Documentation" },
        loadComponent: () =>
          import("./docs/docs.component").then((m) => m.DocsComponent),
      },
      {
        path: "docs/payshield",
        data: { title: "payShield 10K HSM" },
        loadComponent: () =>
          import("./docs/payshield-docs.component").then(
            (m) => m.PayshieldDocsComponent,
          ),
      },
      {
        path: "settings",
        data: { title: "Settings" },
        loadComponent: () =>
          import("./settings/settings.component").then(
            (m) => m.SettingsComponent,
          ),
      },
      {
        path: "trends",
        data: { title: "Trends" },
        loadComponent: () =>
          import("./trends/trends.component").then((m) => m.TrendsComponent),
      },
      {
        path: "schedules",
        data: { title: "Schedules" },
        loadComponent: () =>
          import("./schedules/schedules.component").then(
            (m) => m.SchedulesComponent,
          ),
      },
      {
        // ADR-0007: ad-hoc execution by reference against anything addressable
        path: "playground",
        data: { title: "Playground" },
        loadComponent: () =>
          import("./playground/playground.component").then(
            (m) => m.PlaygroundComponent,
          ),
      },
      {
        path: "simulators",
        data: { title: "Simulators" },
        loadComponent: () =>
          import("./simulators/simulators.component").then(
            (m) => m.SimulatorsComponent,
          ),
      },
      {
        path: "simulators/:id",
        data: { title: "Simulator metrics" },
        loadComponent: () =>
          import("./simulators/simulator-metrics.component").then(
            (m) => m.SimulatorMetricsComponent,
          ),
      },
      {
        path: "peers",
        data: { title: "Live Sessions" },
        loadComponent: () =>
          import("./peers/peers.component").then((m) => m.PeersComponent),
      },
      {
        path: "nats",
        data: { title: "NATS Cluster" },
        loadComponent: () =>
          import("./nats/nats.component").then((m) => m.NatsComponent),
      },
      {
        path: "participants",
        data: { title: "Participant Flows" },
        loadComponent: () =>
          import("./participants/participants.component").then(
            (m) => m.ParticipantsComponent,
          ),
      },
      {
        path: "flow-editor/:id",
        data: { title: "Flow editor", mode: "flow" },
        loadComponent: () =>
          import("./constructor/editor/constructor.component").then(
            (m) => m.ConstructorComponent,
          ),
      },
      {
        path: "network-flows",
        data: { title: "Networks" },
        loadComponent: () =>
          import("./topologies/network-flows.component").then(
            (m) => m.NetworkFlowsComponent,
          ),
      },
      {
        // Legacy: the Topologies manage page was absorbed by Network Flows
        // (ADR-0004). The route redirects so old bookmarks keep working.
        path: "topologies",
        redirectTo: "network-flows",
      },
      {
        // ATLAS §12: the three maps are DIFFERENT jobs, not duplicates —
        // Map 1 is a control board (start/stop/enable across every piece),
        // Map 2 the live diagram, Map 3 replay. Consolidation is naming
        // clarity, not a merge. Old numeric routes redirect to clear names.
        path: "topology-map",
        data: { title: "Network Control" },
        loadComponent: () =>
          import("./topologies/topology-map.component").then(
            (m) => m.TopologyMapComponent,
          ),
      },
      {
        path: "network-map",
        data: { title: "Live Network Map" },
        loadComponent: () =>
          import("./topologies/topology-map2.component").then(
            (m) => m.TopologyMap2Component,
          ),
      },
      {
        // legacy numeric path → the clear name
        path: "topology-map-2",
        redirectTo: "network-map",
      },
      {
        path: "topology-map-3",
        data: { title: "Network Replay" },
        loadComponent: () =>
          import("./topologies/chronoscope/topology-map3.component").then(
            (m) => m.TopologyMap3Component,
          ),
      },
      {
        path: "network-trace",
        data: { title: "Network Trace" },
        loadComponent: () =>
          import("./topologies/network-trace.component").then(
            (m) => m.NetworkTraceComponent,
          ),
      },
      {
        path: "participant-groups",
        data: { title: "Participant Groups" },
        loadComponent: () =>
          import("./groups/groups.component").then((m) => m.GroupsComponent),
      },
      {
        path: "packs",
        data: { title: "Test-case packs" },
        loadComponent: () =>
          import("./packs/packs.component").then((m) => m.PacksComponent),
      },
      {
        path: "integrations",
        data: { title: "Integrations" },
        loadComponent: () =>
          import("./integrations/integrations.component").then(
            (m) => m.IntegrationsComponent,
          ),
      },
      {
        path: "load",
        data: { title: "Load Test" },
        loadComponent: () =>
          import("./load/load.component").then((m) => m.LoadComponent),
      },
      {
        path: "load/:id",
        data: { title: "Load Run" },
        loadComponent: () =>
          import("./load/load-detail.component").then(
            (m) => m.LoadDetailComponent,
          ),
      },
      {
        path: "load-workers",
        data: { title: "Load Workers" },
        loadComponent: () =>
          import("./load/workers.component").then((m) => m.WorkersComponent),
      },
      {
        path: "resilience",
        data: { title: "Resilience" },
        loadComponent: () =>
          import("./resilience/resilience.component").then(
            (m) => m.ResilienceComponent,
          ),
      },
      // Add additional routes here
    ],
  },
];
