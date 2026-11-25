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

import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Input,
  OnChanges,
  SimpleChanges,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentService, PendingChoice } from '../../services/agent.service';

@Component({
  selector: 'app-user-choice-card',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (choice) {
      <div class="user-choice-card" [class.is-stuck]="choice.kind === 'stuck'">
        <div class="uc-header">
          <div class="uc-title-group">
            <span class="material-symbols-outlined uc-icon">
              {{ choice.kind === 'stuck' ? 'help_center' : choice.kind === 'form_options' ? 'check_box' : 'checklist' }}
            </span>
            <div>
              <div class="uc-kicker">
                {{ choice.kind === 'stuck' ? 'AI cần bạn chỉ hướng' : 'AI cần bạn chọn' }}
              </div>
              <div class="uc-question">{{ choice.question }}</div>
            </div>
          </div>
          <span class="uc-timer" title="Thời gian đang chờ bạn trả lời">
            ⏳ đang chờ bạn {{ formattedWait() }}
          </span>
        </div>

        @if (choice.reason) {
          <div class="uc-reason">{{ choice.reason }}</div>
        }

        <div class="uc-body">
          @if (screenshotUrl() && !screenshotBroken()) {
            <div class="uc-thumb-wrap">
              <img
                class="uc-thumb"
                [src]="screenshotUrl()!"
                alt="Màn hình hiện tại"
                (error)="screenshotBroken.set(true)"
              />
            </div>
          }

          <div class="uc-options-col">
            @if (manualWaitingMode()) {
              <div class="uc-manual-box">
                <div class="uc-manual-title">✋ Bạn tự thao tác trên điện thoại</div>
                <p class="uc-manual-hint">
                  Hãy bấm trực tiếp trên điện thoại cho tới màn hình bạn muốn, rồi bấm
                  <strong>Tiếp tục</strong> để AI quan sát lại và làm tiếp.
                </p>
                <div class="uc-actions">
                  <button
                    type="button"
                    class="uc-btn-secondary"
                    [disabled]="isSending()"
                    (click)="manualWaitingMode.set(false)"
                  >
                    Quay lại
                  </button>
                  <button
                    type="button"
                    class="uc-btn-primary"
                    [disabled]="isSending()"
                    (click)="submitManual()"
                  >
                    {{ isSending() ? 'Đang gửi...' : 'Tiếp tục' }}
                  </button>
                </div>
              </div>
            } @else {
              <div class="uc-options-list">
                @if (isMulti()) {
                  <div class="uc-multi-hint">
                    Chọn một hoặc nhiều lựa chọn{{ choice.max_select ? ' (tối đa ' + choice.max_select + ')' : '' }}
                  </div>
                }
                @for (opt of itemOptions(); track opt.id) {
                  <label
                    class="uc-option-row"
                    [class.is-selected]="isMulti() ? selectedItems().includes(opt.id) : selectedOption() === opt.id"
                  >
                    @if (isMulti()) {
                      <input
                        type="checkbox"
                        [value]="opt.id"
                        [checked]="selectedItems().includes(opt.id)"
                        (change)="toggleItem(opt.id)"
                      />
                    } @else {
                      <input
                        type="radio"
                        [name]="'uc-opt-' + choice.choice_id"
                        [value]="opt.id"
                        [checked]="selectedOption() === opt.id"
                        (change)="selectOption(opt.id)"
                      />
                    }
                    <div class="uc-opt-text">
                      <span class="uc-opt-label">{{ opt.id }}. {{ opt.label }}</span>
                      @if (opt.detail) {
                        <span class="uc-opt-detail">{{ opt.detail }}</span>
                      }
                    </div>
                  </label>
                }

                <label class="uc-option-row uc-sys" [class.is-selected]="selectedOption() === 'auto'">
                  <input
                    type="radio"
                    [name]="'uc-opt-' + choice.choice_id"
                    value="auto"
                    [checked]="selectedOption() === 'auto'"
                    (change)="selectOption('auto')"
                  />
                  <div class="uc-opt-text">
                    <span class="uc-opt-label">🤖 {{ autoOptionLabel() }}</span>
                  </div>
                </label>

                <label class="uc-option-row uc-sys" [class.is-selected]="selectedOption() === 'custom'">
                  <input
                    type="radio"
                    [name]="'uc-opt-' + choice.choice_id"
                    value="custom"
                    [checked]="selectedOption() === 'custom'"
                    (change)="selectOption('custom')"
                  />
                  <div class="uc-opt-text uc-custom-wrap">
                    <span class="uc-opt-label">✏️ Yêu cầu khác…</span>
                    <textarea
                      class="uc-custom-input"
                      rows="2"
                      placeholder="Nhập yêu cầu cụ thể cho AI..."
                      [(ngModel)]="customText"
                      (focus)="selectOption('custom')"
                    ></textarea>
                  </div>
                </label>

                <label class="uc-option-row uc-sys" [class.is-selected]="selectedOption() === 'manual'">
                  <input
                    type="radio"
                    [name]="'uc-opt-' + choice.choice_id"
                    value="manual"
                    [checked]="selectedOption() === 'manual'"
                    (change)="enterManualMode()"
                  />
                  <div class="uc-opt-text">
                    <span class="uc-opt-label">✋ Tôi tự thao tác trên điện thoại</span>
                  </div>
                </label>
              </div>

              @if (errorMsg()) {
                <div class="uc-error">{{ errorMsg() }}</div>
              }

              <div class="uc-actions">
                <button
                  type="button"
                  class="uc-btn-primary"
                  [disabled]="!canSubmit() || isSending()"
                  (click)="submit()"
                >
                  {{ isSending() ? 'Đang gửi...' : 'Gửi lựa chọn' }}
                </button>
              </div>
            }
          </div>
        </div>
      </div>
    } @else if (resolvedSummary) {
      <div class="user-choice-resolved">
        <span class="material-symbols-outlined uc-done-icon">check_circle</span>
        <span>Bạn đã chọn: <strong>{{ resolvedSummary.summary }}</strong></span>
      </div>
    }
  `,
  styles: [
    `
      .user-choice-card {
        margin: 14px 0;
        padding: 16px;
        border-radius: 14px;
        background: linear-gradient(180deg, #ffffff 0%, #f8faff 100%);
        border: 1.5px solid #4f7cff;
        box-shadow: 0 6px 20px rgba(79, 124, 255, 0.12);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .user-choice-card.is-stuck {
        border-color: #f59e0b;
        background: linear-gradient(180deg, #ffffff 0%, #fffbeb 100%);
        box-shadow: 0 6px 20px rgba(245, 158, 11, 0.12);
      }
      .uc-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .uc-title-group {
        display: flex;
        align-items: flex-start;
        gap: 10px;
      }
      .uc-icon {
        color: #3b66f5;
        font-size: 22px;
        margin-top: 2px;
      }
      .user-choice-card.is-stuck .uc-icon {
        color: #d97706;
      }
      .uc-kicker {
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #4f7cff;
      }
      .user-choice-card.is-stuck .uc-kicker {
        color: #b45309;
      }
      .uc-question {
        font-size: 15px;
        font-weight: 600;
        color: #0f172a;
        margin-top: 2px;
      }
      .uc-timer {
        font-size: 12px;
        font-weight: 600;
        color: #475569;
        background: #eef2ff;
        padding: 4px 10px;
        border-radius: 999px;
        white-space: nowrap;
      }
      .uc-reason {
        font-size: 12.5px;
        color: #475569;
        background: rgba(15, 23, 42, 0.04);
        padding: 8px 10px;
        border-radius: 8px;
      }
      .uc-body {
        display: flex;
        gap: 14px;
        align-items: flex-start;
      }
      .uc-thumb-wrap {
        flex: 0 0 110px;
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid #e2e8f0;
        background: #0f172a;
      }
      .uc-thumb {
        width: 110px;
        height: auto;
        display: block;
      }
      .uc-options-col {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .uc-options-list {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .uc-option-row {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        padding: 9px 12px;
        border-radius: 10px;
        border: 1px solid #e2e8f0;
        background: #ffffff;
        cursor: pointer;
        transition: border-color 0.15s, background-color 0.15s;
      }
      .uc-option-row:hover {
        border-color: #93c5fd;
        background: #f8fbff;
      }
      .uc-option-row.is-selected {
        border-color: #3b66f5;
        background: #eff6ff;
      }
      .uc-multi-hint {
        font-size: 12px;
        color: #475569;
        margin-bottom: 2px;
      }
      .uc-option-row input[type='checkbox'],
      .uc-option-row input[type='radio'] {
        margin-top: 3px;
      }
      .uc-opt-text {
        display: flex;
        flex-direction: column;
        gap: 2px;
        flex: 1;
        min-width: 0;
      }
      .uc-opt-label {
        font-size: 13.5px;
        font-weight: 600;
        color: #1e293b;
        word-break: break-word;
      }
      .uc-opt-detail {
        font-size: 12px;
        color: #64748b;
      }
      .uc-custom-wrap {
        gap: 6px;
      }
      .uc-custom-input {
        width: 100%;
        box-sizing: border-box;
        padding: 7px 10px;
        border-radius: 8px;
        border: 1px solid #cbd5e1;
        font-family: inherit;
        font-size: 13px;
        resize: vertical;
      }
      .uc-manual-box {
        padding: 12px;
        border-radius: 10px;
        background: #f8fafc;
        border: 1px dashed #64748b;
      }
      .uc-manual-title {
        font-weight: 600;
        font-size: 14px;
        color: #0f172a;
      }
      .uc-manual-hint {
        font-size: 13px;
        color: #475569;
        margin: 6px 0 12px;
      }
      .uc-actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
      }
      .uc-btn-primary {
        padding: 8px 16px;
        border-radius: 8px;
        border: none;
        background: #3b66f5;
        color: #ffffff;
        font-weight: 600;
        font-size: 13px;
        cursor: pointer;
      }
      .uc-btn-primary:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .uc-btn-secondary {
        padding: 8px 14px;
        border-radius: 8px;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        color: #334155;
        font-weight: 500;
        font-size: 13px;
        cursor: pointer;
      }
      .uc-error {
        font-size: 12px;
        color: #dc2626;
      }
      .user-choice-resolved {
        margin: 10px 0;
        padding: 10px 14px;
        border-radius: 10px;
        background: #ecfdf5;
        border: 1px solid #a7f3d0;
        color: #065f46;
        font-size: 13px;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .user-choice-resolved strong {
        color: #047857;
        font-weight: 700;
      }
      .uc-done-icon {
        font-size: 18px;
      }
    `,
  ],
})
export class UserChoiceCardComponent implements OnChanges {
  private agentService = inject(AgentService);
  private destroyRef = inject(DestroyRef);

  @Input() public choice: PendingChoice | null = null;
  @Input() public resolvedSummary: { choice_id: string; summary: string } | null = null;

  public selectedOption = signal<string>('');
  /** Ticked options of a checkbox group (form_options with multi_select). */
  public selectedItems = signal<string[]>([]);
  public customText: string = '';
  public manualWaitingMode = signal<boolean>(false);
  public isSending = signal<boolean>(false);
  public errorMsg = signal<string | null>(null);
  public screenshotBroken = signal<boolean>(false);
  private nowSec = signal<number>(Date.now() / 1000);
  private timerId: any = null;

  public isMulti(): boolean {
    return !!this.choice?.multi_select;
  }

  public toggleItem(id: string): void {
    const cur = this.selectedItems();
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    const max = this.choice?.max_select;
    if (max && next.length > max) {
      this.errorMsg.set(`Chỉ được chọn tối đa ${max} lựa chọn.`);
      return;
    }
    this.selectedItems.set(next);
    this.selectedOption.set(next.length ? 'items' : '');
    this.errorMsg.set(null);
  }

  public itemOptions = computed(() => {
    return (this.choice?.options || []).filter((o) => !o.kind);
  });

  public autoOptionLabel = computed(() => {
    const autoOpt = (this.choice?.options || []).find((o) => o.kind === 'auto');
    if (autoOpt?.label) return autoOpt.label;
    return this.choice?.kind === 'stuck' ? 'Cho AI thử các hướng trên' : 'Để AI tự chọn';
  });

  public screenshotUrl = computed(() => {
    const c = this.choice;
    if (!c?.session_id || !c?.screenshot_path) return null;
    return `/api/tasks/${encodeURIComponent(c.session_id)}/choice/screenshot?c=${encodeURIComponent(c.choice_id)}`;
  });

  public formattedWait = computed(() => {
    const created = this.choice?.created_at;
    const baseWait = this.choice?.waiting_seconds ?? 0;
    const secs = created ? Math.max(0, Math.floor(this.nowSec() - created)) : Math.max(0, Math.floor(baseWait));
    const mins = Math.floor(secs / 60);
    const rem = secs % 60;
    return `${mins}:${rem < 10 ? '0' : ''}${rem}`;
  });

  constructor() {
    this.timerId = setInterval(() => {
      this.nowSec.set(Date.now() / 1000);
    }, 1000);
    this.destroyRef.onDestroy(() => {
      if (this.timerId) {
        clearInterval(this.timerId);
        this.timerId = null;
      }
    });
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['choice']) {
      const prevId = changes['choice'].previousValue?.choice_id;
      const nextId = changes['choice'].currentValue?.choice_id;
      if (prevId !== nextId) {
        this.selectedOption.set('');
        this.selectedItems.set([]);
        this.customText = '';
        this.manualWaitingMode.set(false);
        this.isSending.set(false);
        this.errorMsg.set(null);
        this.screenshotBroken.set(false);
      }
    }
  }

  public selectOption(id: string): void {
    this.selectedOption.set(id);
    if (['auto', 'custom', 'manual'].includes(id)) {
      this.selectedItems.set([]);
    }
    this.errorMsg.set(null);
  }

  public enterManualMode(): void {
    this.selectedOption.set('manual');
    this.manualWaitingMode.set(true);
    this.errorMsg.set(null);
  }

  public canSubmit(): boolean {
    const sel = this.selectedOption();
    if (!sel) return false;
    if (sel === 'custom') {
      return this.customText.trim().length > 0;
    }
    return true;
  }

  public submit(): void {
    const c = this.choice;
    const sel = this.selectedOption();
    if (!c || !sel || this.isSending()) return;
    if (sel === 'manual') {
      this.manualWaitingMode.set(true);
      return;
    }

    let payload: any = {
      session_id: c.session_id || undefined,
      choice_id: c.choice_id,
    };
    let summary = '';
    if (sel === 'auto') {
      payload.type = 'auto';
      summary = this.autoOptionLabel();
    } else if (sel === 'custom') {
      const txt = this.customText.trim();
      if (!txt) {
        this.errorMsg.set('Vui lòng nhập yêu cầu.');
        return;
      }
      payload.type = 'custom';
      payload.text = txt;
      summary = txt;
    } else if (sel === 'items') {
      const ids = this.selectedItems();
      if (!ids.length) return;
      payload.type = 'options';
      payload.option_ids = ids;
      summary = this.itemOptions()
        .filter((o) => ids.includes(o.id))
        .map((o) => `${o.id}. ${o.label}`)
        .join(', ');
    } else {
      payload.type = 'option';
      payload.option_id = sel;
      const found = this.itemOptions().find((o) => o.id === sel);
      summary = found ? `${found.id}. ${found.label}` : sel;
    }

    this.sendAnswer(payload, summary);
  }

  public submitManual(): void {
    const c = this.choice;
    if (!c || this.isSending()) return;
    this.sendAnswer(
      {
        session_id: c.session_id || undefined,
        choice_id: c.choice_id,
        type: 'manual',
      },
      'Tôi tự thao tác trên điện thoại'
    );
  }

  private sendAnswer(payload: any, summary: string): void {
    this.isSending.set(true);
    this.errorMsg.set(null);
    this.agentService.answerChoice(payload, summary).subscribe({
      next: () => {
        this.isSending.set(false);
      },
      error: (err) => {
        this.isSending.set(false);
        this.errorMsg.set(err?.error?.detail || 'Không gửi được câu trả lời. Vui lòng thử lại.');
      },
    });
  }
}
