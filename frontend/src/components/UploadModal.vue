<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";
import { INDUSTRIES, closeUpload, refreshCore, store, toast } from "../store";

const emit = defineEmits<{ uploaded: [] }>();

const kind = ref<"tender" | "bid">("tender");
const files = ref<File[]>([]);
const bind = ref<"none" | "existing" | "new">("none");
const projSel = ref("");
const newName = ref("");
const newInd = ref(INDUSTRIES[0]);
const newNameErr = ref(false);
const projects = ref<{ id: string; name: string }[]>([]);
const fileInput = ref<HTMLInputElement | null>(null);
const busy = ref(false);

const open = computed(() => store.uploadModal.open);

watch(open, async (v) => {
  if (!v) return;
  kind.value = store.uploadModal.kind;
  files.value = [];
  newNameErr.value = false;
  newName.value = "";
  bind.value = "none";
  projSel.value = "";
  try {
    const r = await api.projects("active");
    projects.value = r.items.map((p) => ({ id: p.id, name: p.name }));
  } catch {
    projects.value = [];
  }
  if (store.uploadModal.projectId) {
    bind.value = "existing";
    projSel.value = store.uploadModal.projectId;
  }
});

function pick(): void {
  fileInput.value?.click();
}
function onFiles(ev: Event): void {
  const el = ev.target as HTMLInputElement;
  if (el.files) files.value.push(...Array.from(el.files));
  el.value = "";
}
function fmtF(n: number): string {
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
}
function chooseBind(b: "none" | "existing" | "new"): void {
  bind.value = b;
}

async function save(): Promise<void> {
  if (!files.value.length) {
    toast("请先选择要上传的文件");
    return;
  }
  let projectId: string | null = null;
  if (bind.value === "existing") {
    if (!projSel.value) {
      toast("请选择要绑定的项目");
      return;
    }
    projectId = projSel.value;
  } else if (bind.value === "new") {
    const name = newName.value.trim();
    if (!name) {
      newNameErr.value = true;
      toast("请填写新项目名称");
      return;
    }
    try {
      const r = await api.createProject({ name, industry: newInd.value, role: "主标" });
      projectId = r.id;
    } catch (e) {
      toast(e instanceof Error ? e.message : "创建项目失败");
      return;
    }
  }
  busy.value = true;
  try {
    const r = await api.upload(files.value, kind.value, projectId, true);
    closeUpload();
    toast(
      `已上传 ${r.created.length} 份${kind.value === "tender" ? "招标文件" : "投标文件"}` +
        (projectId ? "，已加入拆解队列" : "，未绑定项目"),
    );
    await refreshCore();
    emit("uploaded");
  } catch (e) {
    toast(e instanceof Error ? e.message : "上传失败");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-if="open" class="modal-wrap" @click.self="closeUpload()">
    <div class="modal" role="dialog" aria-modal="true" aria-label="上传文档">
      <h3>上传文档</h3>
      <p class="m-sub">支持批量上传；招标文件可拆解前附表与章节，投标文件可拆解资格、业绩与团队内容</p>
      <div class="frm">
        <div class="f">
          <span class="fl">文档类型</span>
          <div class="rb">
            <button type="button" :class="{ on: kind === 'tender' }" @click="kind = 'tender'">招标文件</button>
            <button type="button" :class="{ on: kind === 'bid' }" @click="kind = 'bid'">投标文件</button>
          </div>
        </div>
        <div class="f">
          <span class="fl">选择文件</span>
          <div class="upzone" :class="{ hot: files.length === 0 }">
            <div class="uz-t">PDF / Word / 图片</div>
            <div class="uz-s">可多选 · 上传后可在文档库拆解抽取与绑定项目</div>
            <button type="button" class="mini-btn" @click="pick()">选择文件</button>
            <input ref="fileInput" type="file" accept=".pdf,.doc,.docx,.png,.jpg,.jpeg" multiple style="display:none" @change="onFiles">
            <div class="upfiles">
              <span v-for="(f, i) in files" :key="i" class="file-chip">📄 {{ f.name }}<span class="sz">{{ fmtF(f.size) }}</span><button type="button" class="x" @click="files.splice(i, 1)">✕</button></span>
            </div>
          </div>
        </div>
        <div class="f">
          <span class="fl">绑定项目（可稍后在文档库中绑定）</span>
          <div class="bind-opt">
            <div class="bopt" :class="{ on: bind === 'none' }" @click="chooseBind('none')">
              <span class="rd" /><span class="bt"><b>暂不绑定</b><span>仅存入文档库，随时可绑定</span></span>
            </div>
            <div class="bopt" :class="{ on: bind === 'existing' }" @click="chooseBind('existing')">
              <span class="rd" /><span class="bt"><b>绑定到现有项目</b><span>与项目库中的项目关联</span></span>
              <span class="bx" @click.stop><select v-model="projSel"><option value="" disabled>选择项目…</option><option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></span>
            </div>
            <div class="bopt" :class="{ on: bind === 'new' }" @click="chooseBind('new')">
              <span class="rd" /><span class="bt"><b>新建项目并绑定</b><span>快速建档，稍后可在项目库补全</span></span>
              <span class="bx" @click.stop>
                <input v-model="newName" type="text" placeholder="新项目名称 *" :class="{ err: newNameErr }">
                <select v-model="newInd"><option v-for="i in INDUSTRIES" :key="i">{{ i }}</option></select>
              </span>
            </div>
          </div>
        </div>
      </div>
      <div class="m-foot">
        <button class="btn-ghost" @click="closeUpload()">取消</button>
        <button class="btn-primary" :disabled="busy" @click="save">{{ busy ? "上传中…" : "保存" }}</button>
      </div>
    </div>
  </div>
</template>
