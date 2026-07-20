import {
  Component,
  OnInit,
  inject,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import { ThemeService } from "../shared/theme.service";
import { SecretEntry, SecretSource, SecretsService } from "./secrets.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/**
 * Secrets Vault — a masked, read-only inventory of every secret PayProbe holds
 * (connection credentials, secret variables, test-data keys) plus the
 * encryption-at-rest status. Values are never shown; each row deep-links to its
 * owning manager to rotate.
 */
@Component({
  selector: "app-secrets",
  standalone: true,
  imports: [PageHeaderComponent, RouterLink],
  templateUrl: "./secrets.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./secrets.component.scss",
})
export class SecretsComponent implements OnInit {
  readonly theme = inject(ThemeService);
  readonly vault = inject(SecretsService);

  readonly sources: { key: SecretSource; label: string }[] = [
    { key: "connection", label: "Connections" },
    { key: "variable", label: "Variables" },
    { key: "key", label: "Keys" },
  ];

  ngOnInit(): void {
    this.vault.reload();
  }

  rotateRoute(e: SecretEntry): string {
    return this.vault.rotateRoute(e.source);
  }
}
