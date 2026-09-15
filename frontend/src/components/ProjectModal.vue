<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { Project } from "../api/types";
import { INDUSTRIES, PLATFORMS, closeProjectModal, store, toast } from "../store";

const emit = defineEmits<{ saved: [] }>();

const editing = ref<Project | null>(null);
const form = ref({
  name: "",
  role: "主标",
  industry: INDUSTRIES[0],
  open_at: "",
  platform: PLATFORMS[0],
  sales: "",
  cust: "",
  contact: "",
});
const errName = ref(false);
const errOpen = ref(false);
const errCust = ref(false);

/* 招标文件选择（保存时上传入库） */
const filesT = ref<File[]>([]);
const filesN = ref<File[]>([]);
const tenderInput = ref<HTMLInputElement | null>(null);
const noticeInput = ref<HTMLInputElement | null>(null);
const busy = ref(false);

const open = computed(() => store.projectModal.open);

watch(open, async (v) => {
  if (!v) return;
  errName.value = errOpen.value = errCust.value = false;
  filesT.value = [];
  filesN.value = [];
  const id = store.projectModal.editingId;
  if (id) {
    try {
      editing.value = await api.project(id);
      form.value = {
        name: editing.value.name,
        role: editing.value.role,
        industry: editing.value.industry ?? INDUSTRIES[0],
        open_at: (editing.value.open_at ?? "").slice(0, 16).replace(" ", "T"),
        platform: editing.value.platform ?? PLATFORMS[0],
        sales: editing.value.sales ?? "",
        cust: editing.value.cust ?? "",
        contact: editing.value.contact ?? "",
      };
    } catch {
      editing.value = null;
    }
  } else {
    editing.value = null;
    form.value = { name: "", role: "主标", industry: INDUSTRIES[0], open_at: "", platform: PLATFORMS[0], sales: "", cust: "", contact: "" };
  }
});

function pickFiles(kind: "t" | "n"): void {
  (kind === "t" ? tenderInput : noticeInput).value?.click();
}
function onFiles(kind: "t" | "n", ev: Event): void {
  const el = ev.target as HTMLInputElement;
  const list = kind === "t" ? filesT : filesN;
  if (el.files) list.value.push(...Array.from(el.files));
  el.value = "";
}
function fmtF(n: number): string {
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
}

async function save(): Promise<void> {
  errName.value = !form.value.name.trim();
  errOpen.value = !form.value.open_at;
  errCust.value = !form.value.cust.trim();
  if (errName.value || errOpen.value || errCust.value) {
    toast("请补全带 * 的必填项");
    return;
  }
  busy.value = true;
  try {
    const body = { ...form.value, open_at: form.value.open_at.replace("T", " ") };
    let pid: string;
    if (editing.value) {
      await api.updateProject(editing.value.id, body);
      pid = editing.value.id;
      toast(`「${form.value.name}」已更新`);
    } else {
      const r = await api.createProject(body);
      pid = r.id;
      toast(`项目「${form.value.name}」已创建，进入「技术标制作」`);
    }
    if (filesT.value.length) {
      await api.upload(filesT.value, "tender", pid, true);
    }
    if (filesN.value.length) {
      await api.upload(filesN.value, "tender", pid, true);
    }
    closeProjectModal();
    emit("saved");
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  /* watch immediate 不适用（open 初始 false），首次打开由 watch 处理 */
});
</script>

<template>
  <div v-if="open" class="modal-wrap" @click.self="closeProjectModal()">
    <div class="modal" role="dialog" aria-modal="true" aria-label="项目编辑">
      <h3>{{ editing ? "编辑项目" : "新建项目" }}</h3>
      <p class="m-sub">{{ editing ? "修改项目信息与招标资料，保存后立即生效" : "填写基本信息并上传招标资料，创建后进入「技术标制作」阶段" }}</p>
      <div class="frm">
        <div class="frow">
          <div class="f">
            <label class="fl" for="fName">项目名称<span class="req">*</span></label>
            <input id="fName" v-model="form.name" type="text" placeholder="如：智慧园区数据中心项目" :class="{ err: errName }">
            <p class="ferr" :class="{ show: errName }">请填写项目名称</p>
          </div>
          <div class="f">
            <span class="fl">主标 / 陪标</span>
            <div class="rb">
              <button type="button" :class="{ on: form.role === '主标' }" @click="form.role = '主标'">主标</button>
              <button type="button" :class="{ on: form.role === '陪标' }" @click="form.role = '陪标'">陪标</button>
            </div>
          </div>
        </div>
        <div class="frow">
          <div class="f">
            <label class="fl" for="fInd">所属行业</label>
            <select id="fInd" v-model="form.industry">
              <option v-for="i in INDUSTRIES" :key="i">{{ i }}</option>
            </select>
          </div>
          <div class="f">
            <label class="fl" for="fOpen">开标时间<span class="req">*</span></label>
            <input id="fOpen" v-model="form.open_at" type="datetime-local" :class="{ err: errOpen }">
            <p class="ferr" :class="{ show: errOpen }">请选择开标时间</p>
          </div>
        </div>
        <div class="frow">
          <div class="f">
            <label class="fl" for="fPlat">投标平台</label>
            <select id="fPlat" v-model="form.platform">
              <option v-for="p in PLATFORMS" :key="p">{{ p }}</option>
            </select>
          </div>
          <div class="f">
            <label class="fl" for="fSales">负责销售</label>
            <input id="fSales" v-model="form.sales" type="text" placeholder="负责销售姓名">
          </div>
        </div>
        <div class="frow">
          <div class="f">
            <label class="fl" for="fCust">客户名称<span class="req">*</span></label>
            <input id="fCust" v-model="form.cust" type="text" placeholder="招标人 / 采购单位" :class="{ err: errCust }">
            <p class="ferr" :class="{ show: errCust }">请填写客户名称</p>
          </div>
          <div class="f">
            <label class="fl" for="fContact">客户联系人</label>
            <input id="fContact" v-model="form.contact" type="text" placeholder="如：采购部 · 周经理">
          </div>
        </div>
        <div class="frow">
          <div class="f">
            <span class="fl">上传招标文件</span>
            <div class="upzone hot" :class="{ hot: filesT.length === 0 }">
              <div class="uz-t">招标文件</div>
              <div class="uz-s">PDF / Word · 用于废标检测与评分对照</div>
              <button type="button" class="mini-btn" @click="pickFiles('t')">选择文件</button>
              <input ref="tenderInput" type="file" accept=".pdf,.doc,.docx" multiple style="display:none" @change="onFiles('t', $event)">
              <div class="upfiles">
                <span v-for="(f, i) in filesT" :key="i" class="file-chip">📄 {{ f.name }}<span class="sz">{{ fmtF(f.size) }}</span><button type="button" class="x" @click="filesT.splice(i, 1)">✕</button></span>
              </div>
            </div>
          </div>
          <div class="f">
            <span class="fl">上传招标公告</span>
            <div class="upzone" :class="{ hot: filesN.length === 0 }">
              <div class="uz-t">招标公告</div>
              <div class="uz-s">PDF / 图片 · 用于项目信息自动提取</div>
              <button type="button" class="mini-btn" @click="pickFiles('n')">选择文件</button>
              <input ref="noticeInput" type="file" accept=".pdf,.png,.jpg,.jpeg" multiple style="display:none" @change="onFiles('n', $event)">
              <div class="upfiles">
                <span v-for="(f, i) in filesN" :key="i" class="file-chip">📄 {{ f.name }}<span class="sz">{{ fmtF(f.size) }}</span><button type="button" class="x" @click="filesN.splice(i, 1)">✕</button></span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="m-foot">
        <button class="btn-ghost" @click="closeProjectModal()">取消</button>
        <button class="btn-primary" :disabled="busy" @click="save">{{ busy ? "保存中…" : "保存" }}</button>
      </div>
    </div>
  </div>
</template>
