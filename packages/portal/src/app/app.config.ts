import {
  provideHttpClient,
  withInterceptors,
  withXhr,
} from "@angular/common/http";
import { ApplicationConfig } from "@angular/core";
import { provideNativeDateAdapter } from "@angular/material/core";
import { provideAnimations } from "@angular/platform-browser/animations";
import { provideRouter } from "@angular/router";

import { authInterceptor } from "./auth/auth.interceptor";
import { routes } from "./app.routes";

export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(routes),
    provideAnimations(),
    provideHttpClient(withXhr(), withInterceptors([authInterceptor])),
    provideNativeDateAdapter(),
  ],
};
