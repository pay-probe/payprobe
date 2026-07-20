import { HttpErrorResponse, HttpInterceptorFn } from "@angular/common/http";
import { inject } from "@angular/core";
import { Router } from "@angular/router";
import { catchError, throwError } from "rxjs";

import { AuthService } from "./auth.service";

/**
 * Attaches `Authorization: Bearer <token>` to every outgoing API request and,
 * on a 401, clears the session and bounces to the login page (preserving the
 * attempted URL so we can return there after signing in).
 *
 * The login call itself (`/token`) carries no token, so we skip requests that
 * already set Authorization.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  const token = auth.token();
  const authed =
    token && !req.headers.has("Authorization")
      ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } })
      : req;

  return next(authed).pipe(
    catchError((err: HttpErrorResponse) => {
      if (err.status === 401 && auth.isAuthenticated()) {
        auth.logout();
        router.navigate(["/login"], {
          queryParams: { returnUrl: router.url },
        });
      }
      return throwError(() => err);
    }),
  );
};
