from __future__ import annotations
from contextlib import nullcontext
from pathlib import Path
from enum import Enum
from typing import NamedTuple
from PyQt5.QtCore import QObject, pyqtSignal

from . import eventloop, workflow, util
from .settings import settings
from .network import NetworkError
from .image import Extent, Image, Mask, Bounds
from .client import ClientMessage, ClientEvent, filter_supported_styles, resolve_sd_version
from .document import Document, LayerObserver, RestoreActiveLayer
from .pose import Pose
from .style import Style, Styles, SDVersion
from .workflow import ControlMode, Conditioning
from .connection import Connection
from .properties import Property, ObservableProperties
from .jobs import Job, JobKind, JobQueue, JobState
from .control import ControlLayer, ControlLayerList
import krita


class Workspace(Enum):
    generation = 0
    upscaling = 1
    live = 2


class Model(QObject, ObservableProperties):
    """Represents diffusion workflows for a specific Krita document. Stores all inputs related to
    image generation. Launches generation jobs. Listens to server messages and keeps a
    list of finished, currently running and enqueued jobs.
    """

    _doc: Document
    _connection: Connection
    _layer: krita.Node | None = None
    _image_layers: LayerObserver

    workspace = Property(Workspace.generation, setter="set_workspace", persist=True)
    style = Property(Styles.list().default, persist=True)
    prompt = Property("", persist=True)
    negative_prompt = Property("", persist=True)
    control: ControlLayerList
    strength = Property(1.0, persist=True)
    batch_count = Property(1, persist=True)
    seed = Property(0, persist=True)
    fixed_seed = Property(False, persist=True)
    upscale: "UpscaleWorkspace"
    live: "LiveWorkspace"
    progress = Property(0.0)
    jobs: JobQueue
    error = Property("")

    workspace_changed = pyqtSignal(Workspace)
    style_changed = pyqtSignal(Style)
    prompt_changed = pyqtSignal(str)
    negative_prompt_changed = pyqtSignal(str)
    strength_changed = pyqtSignal(float)
    batch_count_changed = pyqtSignal(int)
    seed_changed = pyqtSignal(int)
    fixed_seed_changed = pyqtSignal(bool)
    progress_changed = pyqtSignal(float)
    error_changed = pyqtSignal(str)
    has_error_changed = pyqtSignal(bool)
    modified = pyqtSignal(QObject, str)

    def __init__(self, document: Document, connection: Connection):
        super().__init__()
        self._doc = document
        self._image_layers = document.create_layer_observer()
        self._connection = connection
        self.generate_seed()
        self.jobs = JobQueue()
        self.control = ControlLayerList(self)
        self.upscale = UpscaleWorkspace(self)
        self.live = LiveWorkspace(self)

        self.jobs.selection_changed.connect(self.update_preview)
        self.error_changed.connect(lambda: self.has_error_changed.emit(self.has_error))

        if client := connection.client_if_connected:
            self.style = next(iter(filter_supported_styles(Styles.list(), client)), self.style)
            self.upscale.upscaler = client.default_upscaler

    def generate(self):
        """Enqueue image generation for the current setup."""
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        image = None
        extent = self._doc.extent

        if self._doc.active_layer.type() == "selectionmask":
            mask, image_bounds, selection_bounds = self._doc.create_mask_from_layer(
                settings.selection_padding / 100, is_inpaint=self.strength == 1.0
            )
        else:
            mask, selection_bounds = self._doc.create_mask_from_selection(
                grow=settings.selection_grow / 100,
                feather=settings.selection_feather / 100,
                padding=settings.selection_padding / 100,
                min_size=64,  # minimum size for area conditioning
            )
            image_bounds = workflow.compute_bounds(
                extent, mask.bounds if mask else None, self.strength
            )

        if mask is not None or self.strength < 1.0:
            image = self._get_current_image(image_bounds)
        if selection_bounds is not None:
            selection_bounds = Bounds.apply_crop(selection_bounds, image_bounds)
            selection_bounds = Bounds.minimum_size(selection_bounds, 64, image_bounds.extent)

        control = [c.get_image(image_bounds) for c in self.control]
        conditioning = Conditioning(self.prompt, self.negative_prompt, control)
        conditioning.area = selection_bounds if self.strength == 1.0 else None
        seed = self.seed if self.fixed_seed else workflow.generate_seed()
        generator = self._generate(
            image_bounds, conditioning, self.strength, image, mask, seed, self.batch_count
        )

        self.clear_error()
        eventloop.run(_report_errors(self, generator))

    async def _generate(
        self,
        bounds: Bounds,
        conditioning: Conditioning,
        strength: float,
        image: Image | None,
        mask: Mask | None,
        seed: int = -1,
        count: int = 1,
        is_live=False,
    ):
        client = self._connection.client
        style = self.style
        if not self.jobs.any_executing():
            self.progress = 0.0

        if mask is not None:
            mask_bounds_rel = Bounds(  # mask bounds relative to cropped image
                mask.bounds.x - bounds.x, mask.bounds.y - bounds.y, *mask.bounds.extent
            )
            bounds = mask.bounds  # absolute mask bounds, required to insert result image
            mask.bounds = mask_bounds_rel

        if image is None and mask is None:
            assert strength == 1
            job = workflow.generate(client, style, bounds.extent, conditioning, seed, is_live)
        elif mask is None and strength < 1:
            assert image is not None
            job = workflow.refine(client, style, image, conditioning, strength, seed, is_live)
        elif strength == 1 and not is_live:
            assert image is not None and mask is not None
            job = workflow.inpaint(client, style, image, mask, conditioning, seed)
        else:
            assert image is not None and mask is not None
            job = workflow.refine_region(
                client, style, image, mask, conditioning, strength, seed, is_live
            )

        job_kind = JobKind.live_preview if is_live else JobKind.diffusion
        pos, neg = conditioning.prompt, conditioning.negative_prompt
        for i in range(count):
            job_id = await client.enqueue(job)
            self.jobs.add(job_kind, job_id, pos, neg, bounds, strength, job.seed)
            job.seed = seed + (i + 1) * settings.batch_size

    def upscale_image(self):
        params = self.upscale.params
        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        job = self.jobs.add_upscale(Bounds(0, 0, *self.upscale.target_extent), params.seed)
        self.clear_error()
        eventloop.run(_report_errors(self, self._upscale_image(job, image, params)))

    async def _upscale_image(self, job: Job, image: Image, params: UpscaleParams):
        client = self._connection.client
        upscaler = params.upscaler or client.default_upscaler
        if params.use_diffusion:
            work = workflow.upscale_tiled(
                client, image, upscaler, params.factor, self.style, params.strength, params.seed
            )
        else:
            work = workflow.upscale_simple(client, image, params.upscaler, params.factor)
        job.id = await client.enqueue(work)

    def generate_live(self):
        ver = resolve_sd_version(self.style, self._connection.client)
        image = None

        mask, _ = self._doc.create_mask_from_selection(
            grow=settings.selection_feather / 200,  # don't apply grow for live mode
            feather=settings.selection_feather / 100,
            padding=settings.selection_padding / 100,
            min_size=512 if ver is SDVersion.sd15 else 1024,
            square=True,
        )
        bounds = Bounds(0, 0, *self._doc.extent) if mask is None else mask.bounds
        if mask is not None or self.live.strength < 1.0:
            image = self._get_current_image(bounds)

        control = [c.get_image(bounds) for c in self.control]
        cond = Conditioning(self.prompt, self.negative_prompt, control)
        generator = self._generate(
            bounds, cond, self.live.strength, image, mask, self.seed, count=1, is_live=True
        )

        self.clear_error()
        eventloop.run(_report_errors(self, generator))

    def _get_current_image(self, bounds: Bounds):
        exclude = [  # exclude control layers from projection
            c.layer for c in self.control if c.mode not in [ControlMode.reference, ControlMode.blur]
        ]
        if self._layer:  # exclude preview layer
            exclude.append(self._layer)
        return self._doc.get_image(bounds, exclude_layers=exclude)

    def generate_control_layer(self, control: ControlLayer):
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        mask, _ = self.document.create_mask_from_selection(0, 0, padding=0.25, multiple=64)
        bounds = mask.bounds if mask else None

        job = self.jobs.add_control(control, Bounds(0, 0, *image.extent))
        generator = self._generate_control_layer(job, image, control.mode, bounds)
        self.clear_error()
        eventloop.run(_report_errors(self, generator))
        return job

    async def _generate_control_layer(
        self, job: Job, image: Image, mode: ControlMode, bounds: Bounds | None
    ):
        client = self._connection.client
        work = workflow.create_control_image(client, image, mode, bounds)
        job.id = await client.enqueue(work)

    def cancel(self, active=False, queued=False):
        if queued:
            to_remove = [job for job in self.jobs if job.state is JobState.queued]
            if len(to_remove) > 0:
                self._connection.clear_queue()
                for job in to_remove:
                    self.jobs.remove(job)
        if active and self.jobs.any_executing():
            self._connection.interrupt()

    def report_progress(self, value):
        self.progress = value

    def report_error(self, message: str):
        self.error = message
        self.live.is_active = False

    def clear_error(self):
        if self.error != "":
            self.error = ""

    def handle_message(self, message: ClientMessage):
        job = self.jobs.find(message.job_id)
        if job is None:
            util.client_logger.error(f"Received message {message} for unknown job.")
            return

        if message.event is ClientEvent.progress:
            self.jobs.notify_started(job)
            self.report_progress(message.progress)
        elif message.event is ClientEvent.finished:
            if message.images:
                self.jobs.set_results(job, message.images)
            if job.kind is JobKind.control_layer:
                assert job.control is not None
                job.control.layer_id = self.add_control_layer(job, message.result).uniqueId()
            elif job.kind is JobKind.upscaling:
                self.add_upscale_layer(job)
            self.progress = 1
            self.jobs.notify_finished(job)
            if job.kind is not JobKind.diffusion:
                self.jobs.remove(job)
            elif settings.auto_preview and self._layer is None and job.id:
                self.jobs.select(job.id, 0)
        elif message.event is ClientEvent.interrupted:
            self.jobs.notify_cancelled(job)
            self.report_progress(0)
        elif message.event is ClientEvent.error:
            self.jobs.notify_cancelled(job)
            self.report_error(f"Server execution error: {message.error}")

    def update_preview(self):
        if selection := self.jobs.selection:
            self.show_preview(selection.job, selection.image)
        else:
            self.hide_preview()

    def show_preview(self, job_id: str, index: int, name_prefix="Preview"):
        job = self.jobs.find(job_id)
        assert job is not None, "Cannot show preview, invalid job id"
        name = f"[{name_prefix}] {job.params.prompt}"
        if self._layer and self._layer.parentNode() is None:
            self._layer = None
        if self._layer is not None:
            self._layer.setName(name)
            self._doc.set_layer_content(self._layer, job.results[index], job.params.bounds)
            self._doc.move_to_top(self._layer)
        else:
            self._layer = self._doc.insert_layer(
                name, job.results[index], job.params.bounds, make_active=False
            )
            self._layer.setLocked(True)

    def hide_preview(self):
        if self._layer is not None:
            self._doc.hide_layer(self._layer)

    def apply_result(self, job_id: str, index: int):
        self.jobs.select(job_id, index)
        assert self._layer is not None
        self._layer.setLocked(False)
        self._layer.setName(self._layer.name().replace("[Preview]", "[Generated]"))
        self._doc.active_layer = self._layer
        self._layer = None
        self.jobs.selection = None
        self.jobs.notify_used(job_id, index)

    def add_control_layer(self, job: Job, result: dict | None):
        assert job.kind is JobKind.control_layer and job.control
        if job.control.mode is ControlMode.pose and result is not None:
            pose = Pose.from_open_pose_json(result)
            pose.scale(job.params.bounds.extent)
            return self._doc.insert_vector_layer(job.params.prompt, pose.to_svg())
        elif len(job.results) > 0:
            return self._doc.insert_layer(job.params.prompt, job.results[0], job.params.bounds)
        return self.document.active_layer  # Execution was cached and no image was produced

    def add_upscale_layer(self, job: Job):
        assert job.kind is JobKind.upscaling
        assert len(job.results) > 0, "Upscaling job did not produce an image"
        if self._layer:
            self._layer.remove()
            self._layer = None
        self._doc.resize(job.params.bounds.extent)
        self.upscale.target_extent_changed.emit(self.upscale.target_extent)
        self._doc.insert_layer(job.params.prompt, job.results[0], job.params.bounds)

    def set_workspace(self, workspace: Workspace):
        if self.workspace is Workspace.live:
            self.live.is_active = False
        self._workspace = workspace
        self.workspace_changed.emit(workspace)
        self.modified.emit(self, "workspace")

    def generate_seed(self):
        self.seed = workflow.generate_seed()

    def save_result(self, job_id: str, index: int):
        _save_job_result(self, self.jobs.find(job_id), index)

    @property
    def history(self):
        return (job for job in self.jobs if job.state is JobState.finished)

    @property
    def has_error(self):
        return self.error != ""

    @property
    def document(self):
        return self._doc

    @document.setter
    def document(self, doc):
        # Note: for some reason Krita sometimes creates a new object for an existing document.
        # The old object is deleted and unusable. This method is used to update the object,
        # but doesn't actually change the document identity.
        assert doc == self._doc, "Cannot change document of model"
        self._doc = doc

    @property
    def image_layers(self):
        return self._image_layers


class UpscaleParams(NamedTuple):
    upscaler: str
    factor: float
    use_diffusion: bool
    strength: float
    target_extent: Extent
    seed: int


class UpscaleWorkspace(QObject, ObservableProperties):
    upscaler = Property("", persist=True)
    factor = Property(2.0, persist=True)
    use_diffusion = Property(True, persist=True)
    strength = Property(0.3, persist=True)

    upscaler_changed = pyqtSignal(str)
    factor_changed = pyqtSignal(float)
    use_diffusion_changed = pyqtSignal(bool)
    strength_changed = pyqtSignal(float)
    target_extent_changed = pyqtSignal(Extent)
    modified = pyqtSignal(QObject, str)

    _model: Model

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        if client := model._connection.client_if_connected:
            self.upscaler = client.default_upscaler
        self.factor_changed.connect(lambda _: self.target_extent_changed.emit(self.target_extent))

    @property
    def target_extent(self):
        return self._model.document.extent * self.factor

    @property
    def params(self):
        return UpscaleParams(
            upscaler=self.upscaler,
            factor=self.factor,
            use_diffusion=self.use_diffusion,
            strength=self.strength,
            target_extent=self.target_extent,
            seed=self._model.seed if self._model.fixed_seed else workflow.generate_seed(),
        )


class LiveWorkspace(QObject, ObservableProperties):
    is_active = Property(False, setter="toggle")
    is_recording = Property(False, setter="toggle_record")
    strength = Property(0.3, persist=True)
    has_result = Property(False)

    is_active_changed = pyqtSignal(bool)
    is_recording_changed = pyqtSignal(bool)
    strength_changed = pyqtSignal(float)
    seed_changed = pyqtSignal(int)
    has_result_changed = pyqtSignal(bool)
    result_available = pyqtSignal(Image)
    modified = pyqtSignal(QObject, str)

    _model: Model
    _result: Image | None = None
    _result_bounds: Bounds | None = None
    _recording_layer: krita.Node | None = None
    _keyframes: list[tuple[Image, Bounds]]

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        self._keyframes = []
        model.jobs.job_finished.connect(self.handle_job_finished)

    def toggle(self, active: bool):
        if self.is_active != active:
            self._is_active = active
            self.is_active_changed.emit(active)
            if active:
                self._model.generate_live()
            else:
                self.is_recording = False

    def toggle_record(self, active: bool):
        if self.is_recording != active:
            self._is_recording = active
            self.is_active = active
            self.is_recording_changed.emit(active)
            if active:
                self._add_recording_layer()
            else:
                self._insert_frames()

    def handle_job_finished(self, job: Job):
        if job.kind is JobKind.live_preview:
            if len(job.results) > 0:
                self.set_result(job.results[0], job.params.bounds)
            self.is_active = self._is_active and self._model.document.is_active
            if self.is_active:
                self._model.generate_live()

    def copy_result_to_layer(self):
        assert self.result is not None and self._result_bounds is not None
        doc = self._model.document
        doc.insert_layer(f"[Live] {self._model.prompt}", self.result, self._result_bounds)
        if settings.new_seed_after_apply:
            self._model.generate_seed()

    @property
    def result(self):
        return self._result

    def set_result(self, value: Image, bounds: Bounds):
        self._result = value
        self._result_bounds = bounds
        self.result_available.emit(value)
        self.has_result = True

        if self.is_recording:
            self._keyframes.append((value, bounds))

    def _add_recording_layer(self, restore_active=True):
        doc = self._model.document
        if self._recording_layer and self._recording_layer.parentNode() is None:
            self._recording_layer = None
        if self._recording_layer is None:
            with RestoreActiveLayer(doc) if restore_active else nullcontext():
                self._recording_layer = doc.insert_layer(
                    f"[Recording] {self._model.prompt}", below=doc.active_layer
                )
                self._recording_layer.enableAnimation()
                self._recording_layer.setPinnedToTimeline(True)
        return self._recording_layer

    def _insert_frames(self):
        if len(self._keyframes) > 0:
            layer = self._add_recording_layer(restore_active=False)
            eventloop.run(
                _report_errors(
                    self._model, self._insert_frames_into_timeline(layer, self._keyframes)
                )
            )
            self._keyframes = []

    async def _insert_frames_into_timeline(
        self, layer: krita.Node, frames: list[tuple[Image, Bounds]]
    ):
        doc = self._model.document
        doc.current_time = doc.find_last_keyframe(layer)
        add_keyframe_action = krita.Krita.instance().action("add_duplicate_frame")

        # Try to avoid ASSERT (krita): "row >= 0" in KisAnimCurvesChannelsModel.cpp, line 181
        await eventloop.process_events()

        with RestoreActiveLayer(doc):
            doc.active_layer = layer
            for frame in frames:
                image, bounds = frame
                doc.set_layer_content(layer, image, bounds)
                add_keyframe_action.trigger()
                await eventloop.wait_until(lambda: layer.hasKeyframeAtTime(doc.current_time))
                doc.current_time += 1
        doc.end_time = doc.current_time


async def _report_errors(parent: Model, coro):
    try:
        return await coro
    except NetworkError as e:
        parent.report_error(f"{util.log_error(e)} [url={e.url}, code={e.code}]")
    except Exception as e:
        parent.report_error(util.log_error(e))


def _save_job_result(model: Model, job: Job | None, index: int):
    assert job is not None, "Cannot save result, invalid job id"
    assert len(job.results) > index, "Cannot save result, invalid result index"
    assert model.document.filename, "Cannot save result, document is not saved"
    timestamp = job.timestamp.strftime("%Y%m%d-%H%M%S")
    prompt = util.sanitize_prompt(job.params.prompt)
    path = Path(model.document.filename)
    path = path.parent / f"{path.stem}-generated-{timestamp}-{index}-{prompt}.webp"
    path = util.find_unused_path(path)
    base_image = model._get_current_image(Bounds(0, 0, *model.document.extent))
    result_image = job.results[index]
    base_image.draw_image(result_image, job.params.bounds.offset)
    base_image.save(path)
