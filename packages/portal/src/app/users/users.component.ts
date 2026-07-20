import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { AuthService, UserInfo } from "../auth/auth.service";
import { UsersService } from "../auth/users.service";
import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/** Roles offered in the editor (free-typed roles are still allowed via the API). */
const KNOWN_ROLES = ["admin", "operator", "viewer"];

interface UserDraft {
  username: string;
  roles: string[];
  project_idsText: string; // comma/space separated
  password: string;
  isNew: boolean;
}

/**
 * Users & Roles — admin-only management of auth-service accounts: create users,
 * assign roles + project scope, reset passwords, delete. The auth-service guards
 * against removing the last admin; this page surfaces those errors.
 */
@Component({
  selector: "app-users",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, RouterLink],
  templateUrl: "./users.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./users.component.scss",
})
export class UsersComponent implements OnInit {
  readonly theme = inject(ThemeService);
  readonly auth = inject(AuthService);
  private readonly api = inject(UsersService);
  private readonly ui = inject(UiService);

  readonly knownRoles = KNOWN_ROLES;
  readonly users = signal<UserInfo[]>([]);
  readonly draft = signal<UserDraft | null>(null);
  readonly loading = signal(false);

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.api.list().subscribe({
      next: (u) => {
        this.users.set(u);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.ui.toast("error", "Could not load users (admin only).");
      },
    });
  }

  newUser(): void {
    this.draft.set({
      username: "",
      roles: ["operator"],
      project_idsText: "",
      password: "",
      isNew: true,
    });
  }

  edit(u: UserInfo): void {
    this.draft.set({
      username: u.username,
      roles: [...u.roles],
      project_idsText: u.project_ids.join(", "),
      password: "",
      isNew: false,
    });
  }

  toggleRole(role: string): void {
    const d = this.draft();
    if (!d) return;
    const roles = d.roles.includes(role)
      ? d.roles.filter((r) => r !== role)
      : [...d.roles, role];
    this.draft.set({ ...d, roles });
  }

  isSelf(username: string): boolean {
    return this.auth.user()?.username === username;
  }

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.username.trim())
      return this.ui.toast("error", "Username is required.");
    if (d.isNew && !d.password)
      return this.ui.toast("error", "Set a password.");
    const project_ids = d.project_idsText
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);

    const done = (verb: string) => {
      this.ui.toast("success", `${verb} “${d.username}”`);
      this.draft.set(null);
      this.reload();
    };
    const fail = (e: { error?: { detail?: string } }) =>
      this.ui.toast("error", e?.error?.detail ?? "Save failed.");

    if (d.isNew) {
      this.api
        .create({
          username: d.username,
          password: d.password,
          roles: d.roles,
          project_ids,
        })
        .subscribe({ next: () => done("Created"), error: fail });
    } else {
      this.api
        .update(d.username, {
          roles: d.roles,
          project_ids,
          password: d.password,
        })
        .subscribe({ next: () => done("Updated"), error: fail });
    }
  }

  async remove(u: UserInfo): Promise<void> {
    if (
      !(await this.ui.confirm(`Delete user “${u.username}”?`, { danger: true }))
    )
      return;
    this.api.remove(u.username).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${u.username}”`);
        if (this.draft()?.username === u.username) this.draft.set(null);
        this.reload();
      },
      error: (e) =>
        this.ui.toast("error", e?.error?.detail ?? "Delete failed."),
    });
  }
}
