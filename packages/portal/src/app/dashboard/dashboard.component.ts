import { CommonModule, DatePipe } from "@angular/common";
import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  signal,
} from "@angular/core";
import { RouterLink } from "@angular/router";

export interface TestRun {
  id: string;
  suite: string;
  terminal: string;
  status: "passed" | "failed" | "running";
  passed: number;
  total: number;
  durationSec: number;
  startedAt: Date;
}

export interface SystemService {
  name: string;
  status: "online" | "degraded" | "offline";
  latencyMs: number;
}

export interface StatCard {
  label: string;
  value: string;
  hint: string;
  accent?: boolean;
}

@Component({
  selector: "app-dashboard",
  standalone: true,
  imports: [CommonModule, DatePipe, RouterLink],
  templateUrl: "./dashboard.component.html",
  styleUrl: "./dashboard.component.scss",
})
export class DashboardComponent implements OnInit, OnDestroy {
  private tickerId?: ReturnType<typeof setInterval>;
  private clockId?: ReturnType<typeof setInterval>;
  private progressId?: ReturnType<typeof setInterval>;

  readonly now = signal(new Date());

  private readonly logLines = [
    "0200 MTI auth request → terminal VX-690-014 · F004=000000012050 · approved (00)",
    "0420 reversal advice → terminal PAX-A920-007 · F039=00 · settled",
    "0800 network echo → switch primary · rtt 42ms · ok",
    "0200 MTI auth request → terminal ING-DESK-5000 · F004=000000004999 · declined (51)",
    "0220 offline advice → terminal VX-690-002 · stored-and-forwarded · ok",
  ];
  readonly tickerIndex = signal(0);
  readonly tickerLine = computed(() => this.logLines[this.tickerIndex()]);

  readonly stats = signal<StatCard[]>([
    { label: "Runs today", value: "47", hint: "+12 vs yesterday" },
    { label: "Pass rate (7d)", value: "96.4%", hint: "▲ 1.2pt", accent: true },
    { label: "Active terminals", value: "18", hint: "3 idle" },
    { label: "Avg duration", value: "4m 12s", hint: "per suite" },
  ]);

  readonly liveRun = signal<{
    id: string;
    suite: string;
    terminal: string;
    progress: number;
    step: string;
  } | null>({
    id: "RUN-2616",
    suite: "EMV Contact L2 Regression",
    terminal: "VX-690-014",
    progress: 38,
    step: "Scenario 14/36 · chip_purchase_partial_approval",
  });

  readonly recentRuns = signal<TestRun[]>([
    {
      id: "RUN-2615",
      suite: "NFC Contactless Smoke",
      terminal: "PAX-A920-007",
      status: "passed",
      passed: 24,
      total: 24,
      durationSec: 187,
      startedAt: new Date(Date.now() - 22 * 60_000),
    },
    {
      id: "RUN-2614",
      suite: "ISO 8583 Field Validation",
      terminal: "ING-DESK-5000",
      status: "failed",
      passed: 41,
      total: 44,
      durationSec: 391,
      startedAt: new Date(Date.now() - 71 * 60_000),
    },
    {
      id: "RUN-2613",
      suite: "EMV Contact L2 Regression",
      terminal: "VX-690-002",
      status: "passed",
      passed: 36,
      total: 36,
      durationSec: 248,
      startedAt: new Date(Date.now() - 2.4 * 3_600_000),
    },
    {
      id: "RUN-2612",
      suite: "Reversal & Timeout Handling",
      terminal: "PAX-A920-001",
      status: "passed",
      passed: 18,
      total: 19,
      durationSec: 322,
      startedAt: new Date(Date.now() - 3.1 * 3_600_000),
    },
    {
      id: "RUN-2611",
      suite: "Refund Flow E2E",
      terminal: "VX-690-014",
      status: "failed",
      passed: 9,
      total: 12,
      durationSec: 154,
      startedAt: new Date(Date.now() - 5 * 3_600_000),
    },
  ]);

  readonly services = signal<SystemService[]>([
    { name: "xPay Gateway", status: "online", latencyMs: 31 },
    { name: "HSM", status: "online", latencyMs: 8 },
    { name: "Core Banking Mock", status: "online", latencyMs: 54 },
    { name: "Switch", status: "degraded", latencyMs: 212 },
    { name: "Acquirer Sim", status: "online", latencyMs: 47 },
    { name: "Report Storage", status: "online", latencyMs: 19 },
  ]);

  readonly healthSummary = computed(() => {
    const down = this.services().filter((s) => s.status !== "online").length;
    return down === 0 ? "All systems operational" : `${down} service(s) degraded`;
  });

  ngOnInit(): void {
    this.clockId = setInterval(() => this.now.set(new Date()), 1000);
    this.tickerId = setInterval(
      () => this.tickerIndex.update((i) => (i + 1) % this.logLines.length),
      2200,
    );
    this.progressId = setInterval(() => {
      const run = this.liveRun();
      if (!run) return;
      const next = Math.min(run.progress + Math.random() * 3, 100);
      this.liveRun.set({ ...run, progress: next });
    }, 1500);
  }

  ngOnDestroy(): void {
    clearInterval(this.tickerId);
    clearInterval(this.clockId);
    clearInterval(this.progressId);
  }

  passRate(run: TestRun): number {
    return Math.round((run.passed / run.total) * 100);
  }

  duration(sec: number): string {
    const m = Math.floor(sec / 60);
    return `${m}m ${sec % 60}s`;
  }

  timeAgo(d: Date): string {
    const mins = Math.round((Date.now() - d.getTime()) / 60_000);
    if (mins < 60) return `${mins}m ago`;
    const h = Math.floor(mins / 60);
    return `${h}h ${mins % 60}m ago`;
  }
}
