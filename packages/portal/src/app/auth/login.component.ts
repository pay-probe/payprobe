import {
  Component,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, Router } from "@angular/router";

import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { AuthService } from "./auth.service";

@Component({
  selector: "pp-login",
  standalone: true,
  imports: [FormsModule, ButtonComponent, IconComponent],
  template: `
    <div class="login-wrap">
      <form class="login-card" (ngSubmit)="submit()">
        <div class="brand">
          <span class="brand__mark"><pp-icon name="shield" [size]="22" /></span>
          <div>
            <h1 class="brand__name">PayProbe</h1>
            <p class="brand__sub">Sign in to your workspace</p>
          </div>
        </div>

        <label class="field">
          <span class="field__label">Username</span>
          <input
            class="field__input"
            name="username"
            autocomplete="username"
            [(ngModel)]="username"
            [disabled]="loading()"
            required
            autofocus
          />
        </label>

        <label class="field">
          <span class="field__label">Password</span>
          <input
            class="field__input"
            name="password"
            type="password"
            autocomplete="current-password"
            [(ngModel)]="password"
            [disabled]="loading()"
            required
          />
        </label>

        @if (error()) {
          <p class="error" role="alert">
            <pp-icon name="alert-circle" [size]="16" /> {{ error() }}
          </p>
        }

        <pp-button
          type="submit"
          variant="primary"
          size="lg"
          [disabled]="loading() || !username || !password"
        >
          {{ loading() ? "Signing in…" : "Sign in" }}
        </pp-button>
      </form>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      .login-wrap {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 100vh;
        padding: var(--space-6);
        background: var(--bg-base);
      }
      .login-card {
        width: 100%;
        max-width: 380px;
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
        padding: var(--space-7) var(--space-6);
        background: var(--bg-surface);
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-lg);
      }
      .brand {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        margin-bottom: var(--space-2);
      }
      .brand__mark {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: var(--radius-md);
        background: var(--brand);
        color: var(--text-on-brand);
      }
      .brand__name {
        margin: 0;
        font-size: var(--text-lg);
        font-weight: var(--weight-bold);
        color: var(--text-primary);
      }
      .brand__sub {
        margin: 2px 0 0;
        font-size: var(--text-sm);
        color: var(--text-secondary);
      }
      .field {
        display: flex;
        flex-direction: column;
        gap: var(--space-1-5);
      }
      .field__label {
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
        color: var(--text-secondary);
      }
      .field__input {
        font-family: inherit;
        font-size: var(--text-sm);
        padding: var(--space-2-5) var(--space-3);
        color: var(--text-primary);
        background: var(--bg-base);
        border: 1px solid var(--border-strong);
        border-radius: var(--radius-md);
        transition:
          border-color var(--duration-fast) var(--ease-out),
          box-shadow var(--duration-fast) var(--ease-out);
      }
      .field__input:focus {
        outline: none;
        border-color: var(--brand);
        box-shadow: 0 0 0 3px var(--focus-ring);
      }
      .error {
        display: flex;
        align-items: center;
        gap: var(--space-1-5);
        margin: 0;
        font-size: var(--text-sm);
        color: var(--color-danger);
      }
      pp-button {
        margin-top: var(--space-2);
      }
      pp-button > * {
        width: 100%;
      }
    `,
  ],
})
export class LoginComponent {
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  username = "";
  password = "";
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  submit(): void {
    if (!this.username || !this.password || this.loading()) return;
    this.loading.set(true);
    this.error.set(null);
    this.auth.login(this.username, this.password).subscribe({
      next: () => {
        const returnUrl =
          this.route.snapshot.queryParamMap.get("returnUrl") || "/dashboard";
        this.router.navigateByUrl(returnUrl);
      },
      error: (err) => {
        this.loading.set(false);
        this.error.set(
          err?.status === 401
            ? "Invalid username or password."
            : "Could not reach the auth service. Try again.",
        );
      },
    });
  }
}
