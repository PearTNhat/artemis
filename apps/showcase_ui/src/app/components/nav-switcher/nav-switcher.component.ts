/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Component, ChangeDetectionStrategy, inject, OnInit } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { SystemService } from '../../services/system.service';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="floating-nav-switcher" aria-label="Main Navigation">
      <a 
        routerLink="/" 
        routerLinkActive="active" 
        [routerLinkActiveOptions]="{exact: true}"
        class="nav-tab-btn"
        title="Return to Home Launcher to start a new task"
      >
        <span class="material-symbols-outlined tab-icon">add_task</span>
        <span class="tab-label">New / Home</span>
      </a>
      <a 
        routerLink="/workspace" 
        routerLinkActive="active" 
        class="nav-tab-btn"
        title="Open Workspace"
      >
        <span class="material-symbols-outlined tab-icon">space_dashboard</span>
        <span class="tab-label">Workspace</span>
      </a>

      <div class="nav-divider"></div>
      @if (systemService.usesCliModel()) {
        <!-- The agent reasons through the agy CLI: one badge says it all. -->
        <a
          routerLink="/"
          [queryParams]="{tab: 'diagnostics'}"
          class="nav-cli-badge is-gemini"
          [title]="'AI đang chạy qua CLI: ' + systemService.activeModelDisplayName() + '. Bấm để đổi model.'"
        >
          <span class="cli-badge-icon">🤖</span>
          <span class="cli-badge-name">CLI: {{ systemService.activeModelDisplayName() }}</span>
        </a>
      } @else if (systemService.activeModelDisplayName()) {
        <a
          routerLink="/"
          [queryParams]="{tab: 'diagnostics'}"
          class="nav-model-badge"
          [title]="'Vision Model: ' + systemService.activeModelDisplayName() + ' (' + systemService.activeModelName() + '). Bấm để xem hoặc cấu hình model.'"
        >
          <span class="model-badge-icon">{{ systemService.activeModelBadgeIcon() }}</span>
          <span class="model-badge-name">{{ systemService.activeModelDisplayName() }}</span>
        </a>
      }
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent implements OnInit {
  public systemService = inject(SystemService);

  ngOnInit(): void {
    if (!this.systemService.modelConfigEnv()) {
      this.systemService.fetchModelConfigEnv().subscribe();
    }
  }
}
