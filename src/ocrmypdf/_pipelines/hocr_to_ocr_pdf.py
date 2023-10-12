# SPDX-FileCopyrightText: 2019-2022 James R. Barlow
# SPDX-FileCopyrightText: 2019 Martin Wind
# SPDX-License-Identifier: MPL-2.0

"""Implements the concurrent and page synchronous parts of the pipeline."""


from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import threading
from collections.abc import Sequence
from functools import partial

import PIL

from ocrmypdf._concurrent import Executor
from ocrmypdf._graft import OcrGrafter
from ocrmypdf._jobcontext import PageContext, PdfContext
from ocrmypdf._pipeline import (
    copy_final,
    get_pdfinfo,
    render_hocr_page,
    validate_pdfinfo_options,
)
from ocrmypdf._pipelines.common import (
    HOCRResult,
    post_process,
    report_output_pdf,
    set_logging_tls,
    setup_pipeline,
    worker_init,
)
from ocrmypdf._plugin_manager import OcrmypdfPluginManager

log = logging.getLogger(__name__)


tls = threading.local()
tls.pageno = None

set_logging_tls(tls)


def exec_hocrtransform_sync(page_context: PageContext) -> HOCRResult:
    hocr_result = HOCRResult.from_json(page_context.get_path('hocr.json').read_text())
    hocr_result.textpdf = render_hocr_page(
        page_context.get_path('ocr_hocr.hocr'), page_context
    )
    return hocr_result


def exec_hocr_to_ocr_pdf(context: PdfContext, executor: Executor) -> None:
    """Execute the OCR pipeline concurrently and output hOCR."""
    # Run exec_page_sync on every page
    options = context.options
    max_workers = min(len(context.pdfinfo), options.jobs)
    if max_workers > 1:
        log.info("Continue processing %d pages concurrently", max_workers)

    ocrgraft = OcrGrafter(context)

    def graft_page(result: HOCRResult, pbar):
        """After OCR is complete for a page, update the PDF."""
        try:
            tls.pageno = result.pageno + 1
            pbar.update()
            ocrgraft.graft_page(
                pageno=result.pageno,
                image=result.pdf_page_from_image,
                textpdf=result.textpdf,
                autorotate_correction=result.orientation_correction,
            )
            pbar.update()
        finally:
            tls.pageno = None

    executor(
        use_threads=options.use_threads,
        max_workers=max_workers,
        tqdm_kwargs=dict(
            total=(2 * len(context.pdfinfo)),
            desc='Grafting hOCR to PDF',
            unit='page',
            unit_scale=0.5,
            disable=not options.progress_bar,
        ),
        worker_initializer=partial(worker_init, PIL.Image.MAX_IMAGE_PIXELS),
        task=exec_hocrtransform_sync,
        task_arguments=context.get_page_contexts(),
        task_finished=graft_page,
    )

    pdf = ocrgraft.finalize()
    messages: Sequence[str] = []
    if options.output_type != 'none':
        # PDF/A and metadata
        log.info("Postprocessing...")
        pdf, messages = post_process(pdf, context, executor)

        # Copy PDF file to destination
        copy_final(pdf, options.output_file, context)
    return messages


def run_hocr_to_ocr_pdf_pipeline(
    options: argparse.Namespace,
    *,
    plugin_manager: OcrmypdfPluginManager | None,
) -> None:
    work_folder, debug_log_handler, executor, plugin_manager = setup_pipeline(
        options=options,
        plugin_manager=plugin_manager,
        api=True,
        work_folder=options.output_folder,
    )

    origin_pdf = work_folder / 'origin.pdf'
    shutil.copy2(options.input_file, origin_pdf)

    # Gather pdfinfo and create context
    pdfinfo = get_pdfinfo(
        options.input_file,
        executor=executor,
        detailed_analysis=options.redo_ocr,
        progbar=options.progress_bar,
        max_workers=options.jobs if not options.use_threads else 1,  # To help debug
        check_pages=options.pages,
    )
    context = PdfContext(
        options, work_folder, options.input_file, pdfinfo, plugin_manager
    )
    # Validate options are okay for this pdf
    validate_pdfinfo_options(context)
    optimize_messages = exec_hocr_to_ocr_pdf(context, executor)

    report_output_pdf(options, origin_pdf, optimize_messages)
