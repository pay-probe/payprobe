import { inject } from "@angular/core";
import { CanActivateChildFn, Router } from "@angular/router";

import { AuthService } from "./auth.service";

/**
 * Gates the authenticated shell. Unauthenticated visitors are redirected to
 * the login page with the attempted URL as `returnUrl`.
 */
export const authGuard: CanActivateChildFn = (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if (auth.isAuthenticated()) return true;

  return router.createUrlTree(["/login"], {
    queryParams: { returnUrl: state.url },
  });
};
