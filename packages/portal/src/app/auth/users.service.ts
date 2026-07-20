import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { UserInfo } from "./auth.service";

export interface CreateUserRequest {
  username: string;
  password: string;
  roles: string[];
  project_ids: string[];
}

export interface UpdateUserRequest {
  roles: string[];
  project_ids: string[];
  /** Blank keeps the existing password. */
  password: string;
}

/** Typed client for auth-service's admin-gated user management endpoints. */
@Injectable({ providedIn: "root" })
export class UsersService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.authApiBase;
  }

  list(): Observable<UserInfo[]> {
    return this.http.get<UserInfo[]>(`${this.base}/users`);
  }

  create(req: CreateUserRequest): Observable<UserInfo> {
    return this.http.post<UserInfo>(`${this.base}/users`, req);
  }

  update(username: string, req: UpdateUserRequest): Observable<UserInfo> {
    return this.http.put<UserInfo>(
      `${this.base}/users/${encodeURIComponent(username)}`,
      req,
    );
  }

  remove(username: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/users/${encodeURIComponent(username)}`,
    );
  }
}
