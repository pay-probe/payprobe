import { inject } from "@angular/core";
import { CanActivateFn, Router } from "@angular/router";

import { AuthService } from "./auth.service";

/**
 * Gates admin-only routes (e.g. Users & Roles). Authenticated non-admins are
 * sent back to the dashboard; unauthenticated visitors hit the auth guard first.
 */
export const adminGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if ((auth.user()?.roles ?? []).includes("admin")) return true;
  return router.createUrlTree(["/dashboard"]);
};
