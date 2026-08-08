/*
 * storage_picker.js — 统一目录选择器（存储管理 / 同步管理共用）
 *
 * 两种后端：
 *   - backend="storage"：经 mounts + list 端点浏览存储后端虚拟路径（含挂载名）。
 *     根目录 = 挂载列表；进入某挂载后路径形如 /挂载名/子目录。
 *   - backend="local"：经 local 端点浏览服务器本地绝对路径（用于新增 local 挂载的 root_path）。
 *
 * 交互：点击文件夹进入下一级；搜索框过滤当前目录文件夹；「选定此目录」把当前路径
 * 回填到 targetInputId 对应的输入框，并写入状态提示。
 */
(function (global) {
  'use strict';

  function StoragePicker(opts) {
    this.uid = opts.uid;
    this.targetInputId = opts.targetInputId;
    this.backend = opts.backend || 'storage';
    this.mountsEndpoint = opts.mountsEndpoint;
    this.listEndpoint = opts.listEndpoint;
    this.initialPath = opts.initialPath || (this.backend === 'local' ? '/' : '/');

    // DOM
    this.modal = document.getElementById(this.uid + '-modal');
    this.listEl = document.getElementById(this.uid + '-list');
    this.crumbEl = document.getElementById(this.uid + '-breadcrumb');
    this.searchEl = document.getElementById(this.uid + '-search');
    this.statusEl = document.getElementById(this.uid + '-status');
    this.confirmBtn = document.getElementById(this.uid + '-confirm');
    this.selectionHint = document.getElementById(this.uid + '-selection-hint');
    this.openBtn = document.getElementById(opts.openBtnId || (this.uid + '-open'));
    this.targetInput = this.targetInputId ? document.getElementById(this.targetInputId) : null;

    // 状态
    this.currentPath = this.initialPath || (this.backend === 'local' ? '/' : '/');
    this.currentDirs = [];   // 当前目录文件夹名列表
    this.selectedPath = this.targetInput && this.targetInput.value ? this.targetInput.value : '';

    this._bind();
  }

  StoragePicker.prototype._bind = function () {
    var self = this;
    if (this.openBtn) {
      this.openBtn.addEventListener('click', function () { self.open(); });
    }
    var close = document.getElementById(this.uid + '-close');
    var cancel = document.getElementById(this.uid + '-cancel');
    if (close) close.addEventListener('click', function () { self.close(); });
    if (cancel) cancel.addEventListener('click', function () { self.close(); });
    if (this.modal) {
      this.modal.addEventListener('click', function (e) {
        if (e.target === self.modal) self.close();
      });
      // 初始保证隐藏（CSS 通过 .open 类控制 display）。
      this.modal.classList.remove('open');
      this.modal.hidden = true;
    }
    if (this.confirmBtn) {
      this.confirmBtn.addEventListener('click', function () { self.confirm(); });
    }
    var up = document.getElementById(this.uid + '-up');
    if (up) up.addEventListener('click', function () { self.goUp(); });
    if (this.searchEl) {
      this.searchEl.addEventListener('input', function () { self.renderList(); });
    }
  };

  StoragePicker.prototype.open = function () {
    // 以目标输入框已有值为起点（若有），否则用初始路径。
    var start = (this.targetInput && this.targetInput.value) ? this.targetInput.value
              : this.initialPath || (this.backend === 'local' ? '/' : '/');
    this.currentPath = start;
    this.selectedPath = start;
    // 模态框显示由 CSS `.modal-mask.open` 控制（原生 hidden 属性会被 display:none 覆盖）。
    if (this.modal) {
      this.modal.hidden = false;
      this.modal.classList.add('open');
    }
    if (this.searchEl) this.searchEl.value = '';
    this.load();
  };

  StoragePicker.prototype.close = function () {
    if (this.modal) {
      this.modal.classList.remove('open');
      this.modal.hidden = true;
    }
  };

  StoragePicker.prototype._fetch = function (url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return { items: [], error: '响应解析失败' }; });
    }).catch(function (e) {
      return { items: [], error: String(e) };
    });
  };

  StoragePicker.prototype.load = function () {
    var self = this;
    // storage 后端根目录（路径为 "/" 或空）先取挂载列表；其余走 list。
    var isRoot = (this.currentPath === '/' || this.currentPath === '' || this.currentPath === null);
    if (this.backend === 'storage' && isRoot) {
      this._fetch(this.mountsEndpoint).then(function (data) {
        self.currentDirs = (data.items || []).map(function (i) { return i.name; });
        self.renderList();
        self.renderCrumb();
      });
    } else {
      var sep = this.listEndpoint.indexOf('?') >= 0 ? '&' : '?';
      var url = this.listEndpoint + sep + 'path=' + encodeURIComponent(this.currentPath);
      this._fetch(url).then(function (data) {
        if (data.error) {
          self.currentDirs = [];
          self.renderList(data.error);
        } else {
          self.currentDirs = (data.items || [])
            .filter(function (i) { return i.is_dir; })
            .map(function (i) { return i.name; });
          self.renderList();
        }
        self.renderCrumb();
      });
    }
  };

  StoragePicker.prototype._join = function (child) {
    if (this.backend === 'local') {
      // 本地绝对路径拼接
      var base = this.currentPath || '/';
      if (base === '/') return '/' + child;
      return base.replace(/\/$/, '') + '/' + child;
    }
    // 虚拟路径拼接（POSIX）
    var v = this.currentPath || '/';
    if (v === '/') return '/' + child;
    return v.replace(/\/$/, '') + '/' + child;
  };

  StoragePicker.prototype.goUp = function () {
    if (this.backend === 'local') {
      if (this.currentPath === '/' || this.currentPath === '') return;
      this.currentPath = this.currentPath.replace(/\/$/, '');
      var idx = this.currentPath.lastIndexOf('/');
      this.currentPath = idx <= 0 ? '/' : this.currentPath.slice(0, idx);
    } else {
      if (this.currentPath === '/' || this.currentPath === '') return;
      var v = this.currentPath.replace(/\/$/, '');
      var i = v.lastIndexOf('/');
      this.currentPath = (i <= 0) ? '/' : v.slice(0, i);
    }
    this.selectedPath = this.currentPath;
    this.load();
  };

  StoragePicker.prototype.enter = function (name) {
    this.currentPath = this._join(name);
    this.selectedPath = this.currentPath;
    this.load();
  };

  StoragePicker.prototype.renderCrumb = function () {
    if (!this.crumbEl) return;
    var self = this;
    this.crumbEl.innerHTML = '';
    var parts;
    if (this.backend === 'local') {
      parts = (this.currentPath || '/').split('/').filter(Boolean);
      var acc = '';
      var mk = function (label, path) {
        var a = document.createElement('a');
        a.href = 'javascript:void(0)';
        a.textContent = label;
        a.addEventListener('click', function () { self.currentPath = path; self.selectedPath = path; self.load(); });
        return a;
      };
      this.crumbEl.appendChild(mk('根', '/'));
      parts.forEach(function (p) {
        acc += '/' + p;
        self.crumbEl.appendChild(document.createTextNode(' / '));
        self.crumbEl.appendChild(mk(p, acc));
      });
    } else {
      parts = (this.currentPath || '/').split('/').filter(Boolean);
      var v = '';
      var mkv = function (label, path) {
        var a = document.createElement('a');
        a.href = 'javascript:void(0)';
        a.textContent = label;
        a.addEventListener('click', function () { self.currentPath = path; self.selectedPath = path; self.load(); });
        return a;
      };
      this.crumbEl.appendChild(mkv('根', '/'));
      parts.forEach(function (p) {
        v += '/' + p;
        self.crumbEl.appendChild(document.createTextNode(' / '));
        self.crumbEl.appendChild(mkv(p, v));
      });
    }
  };

  StoragePicker.prototype.renderList = function (errorMsg) {
    if (!this.listEl) return;
    this.listEl.innerHTML = '';
    if (errorMsg) {
      var err = document.createElement('div');
      err.className = 'empty';
      err.textContent = '加载失败：' + errorMsg;
      this.listEl.appendChild(err);
      this._updateSelection();
      return;
    }
    var self = this;
    var q = (this.searchEl && this.searchEl.value || '').trim().toLowerCase();
    var dirs = this.currentDirs.filter(function (d) {
      return !q || d.toLowerCase().indexOf(q) >= 0;
    });
    if (!dirs.length) {
      var empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = q ? '没有匹配的文件夹' : '该目录下没有子文件夹';
      this.listEl.appendChild(empty);
      this._updateSelection();
      return;
    }
    var ul = document.createElement('ul');
    ul.className = 'fp-list';
    dirs.forEach(function (name) {
      var li = document.createElement('li');
      li.className = 'fp-item';
      li.textContent = '📁 ' + name;
      li.addEventListener('click', function () { self.enter(name); });
      ul.appendChild(li);
    });
    this.listEl.appendChild(ul);
    this._updateSelection();
  };

  StoragePicker.prototype._updateSelection = function () {
    if (this.selectionHint) {
      this.selectionHint.textContent = '当前目录：' + (this.currentPath || '/');
    }
    if (this.confirmBtn) {
      this.confirmBtn.disabled = !(this.currentPath && this.currentPath.length);
    }
  };

  StoragePicker.prototype.confirm = function () {
    var path = this.currentPath || '/';
    if (this.targetInput) this.targetInput.value = path;
    if (this.statusEl) {
      this.statusEl.textContent = '已选择：' + path;
    }
    // 触发 input 事件，便于外部表单联动（如自动注入 JSON）。
    if (this.targetInput) {
      this.targetInput.dispatchEvent(new Event('input', { bubbles: true }));
      this.targetInput.dispatchEvent(new Event('change', { bubbles: true }));
    }
    this.close();
  };

  global.StoragePicker = StoragePicker;
})(window);
