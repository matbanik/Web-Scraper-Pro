# ============================================================================
# Web Scraper Pro
# Version: 7.19 (Enhanced Third-Party Logging Control)
# Last Modified: 11:30 AM EDT, September 8, 2025
# Dependencies: This script is packaged using PyInstaller. The build process
#               is controlled by 'build.bat'.
# ============================================================================

import sys
import os
import re
import json
import time
import traceback
import zipfile
import shutil
import logging
from urllib.parse import urlparse, urljoin
from pathlib import Path
from multiprocessing import Process, Queue, freeze_support
from random import choice
import html
import threading
import requests

# --- Scraping & Parsing Imports ---
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.spiders import CrawlSpider, Rule
from scrapy.linkextractors import LinkExtractor
from scrapy.exceptions import CloseSpider, DropItem
from bs4 import BeautifulSoup
import tldextract
import trafilatura

# --- PDF Generation Imports ---
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

# List of user agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
]

# Optional: List of proxies
PROXIES = []

# =============================================================================
# 0. ENHANCED LOGGING SETUP
# =============================================================================

class QueueHandler(logging.Handler):
    """
    A custom logging handler that puts logs into a multiprocessing queue.
    """
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        try:
            # Put the formatted log message into the queue
            self.queue.put(('log', self.format(record)))
        except Exception:
            self.handleError(record)

def configure_third_party_loggers(log_level_str):
    """Configure third-party library loggers based on the selected log level."""
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    
    # Define third-party loggers that should be controlled
    third_party_loggers = [
        'scrapy',
        'scrapy.core.engine',
        'scrapy.crawler',
        'scrapy.extensions.telnet',
        'scrapy.middleware',
        'scrapy.utils.log',
        'scrapy.statscollectors',
        'scrapy.extensions.logstats',
        'requests',
        'requests.packages.urllib3',
        'urllib3',
        'urllib3.connectionpool',
        'urllib3.util.retry',
        'reportlab',
        'trafilatura',
        'tldextract',
        'selenium',  # In case you add selenium later
        'twisted',   # Scrapy uses Twisted
    ]
    
    # If log level is DEBUG, allow all loggers to be verbose
    if log_level <= logging.DEBUG:
        for logger_name in third_party_loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.DEBUG)
        logging.debug("All third-party loggers set to DEBUG level")
    else:
        # For INFO and above, silence most third-party loggers
        for logger_name in third_party_loggers:
            logger = logging.getLogger(logger_name)
            if log_level >= logging.WARNING:
                # Only show warnings and errors
                logger.setLevel(logging.WARNING)
            else:
                # For INFO level, show minimal info from third parties
                logger.setLevel(logging.WARNING)
        
        # Special handling for specific noisy loggers
        logging.getLogger('scrapy.core.engine').setLevel(logging.ERROR)
        logging.getLogger('scrapy.extensions.logstats').setLevel(logging.ERROR)
        logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
        
        logging.debug(f"Third-party loggers configured for {log_level_str} level")

def setup_worker_logging(queue):
    """Enhanced logger setup that controls third-party library logging."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
    
    queue_handler = QueueHandler(queue)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
    queue_handler.setFormatter(formatter)
    root_logger.addHandler(queue_handler)
    
    # Set root to lowest level; individual loggers will be controlled below
    root_logger.setLevel(logging.DEBUG)

def setup_main_logging(queue, log_level_str):
    """Setup logging for the main GUI process with third-party control."""
    root_logger = logging.getLogger()
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    queue_handler = QueueHandler(queue)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
    queue_handler.setFormatter(formatter)
    root_logger.addHandler(queue_handler)
    
    # Configure third-party loggers
    configure_third_party_loggers(log_level_str)
    
    logging.info("Application logger initialized. Logs will appear in the GUI.")

# =============================================================================
# 1. SCRAPY COMPONENTS
# =============================================================================
class RotateUserAgentAndProxyMiddleware:
    def process_request(self, request, spider):
        request.headers['User-Agent'] = choice(USER_AGENTS)
        if PROXIES:
            request.meta['proxy'] = choice(PROXIES)
        return None
    def process_response(self, request, response, spider):
        if response.status == 403:
            logging.warning(f"403 Forbidden: {response.url}. Retrying...")
            return request.replace(dont_filter=True)
        return response
    def process_exception(self, request, exception, spider):
        logging.error(f"Downloader exception on {request.url}: {exception}")
        return None

class ScrapedDataItem(scrapy.Item):
    source_url = scrapy.Field()
    content = scrapy.Field()

class FilePipeline:
    def __init__(self, queue):
        self.queue = queue
        self.completed_urls = set()
        self.domain_name = None
        self.data_folder = None
        self.progress_file = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.spider.queue)

    def open_spider(self, spider):
        extracted = tldextract.extract(spider.start_urls[0])
        self.domain_name = extracted.domain
        if not self.domain_name:
            self.domain_name = extracted.subdomain or "output"

        self.data_folder = Path(spider.output_path) / self.domain_name
        self.data_folder.mkdir(exist_ok=True, parents=True)
        spider.data_folder = self.data_folder

        self.progress_file = self.data_folder / f"progress_{self.domain_name}.json"
        if self.progress_file.exists():
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.completed_urls = set(data.get('completed_urls', []))
                logging.info(f"Resuming scrape. Found {len(self.completed_urls)} completed URLs.")
            except (json.JSONDecodeError, IOError) as e:
                logging.error(f"Could not read progress file: {e}")
        
        spider.completed_urls = self.completed_urls

    def close_spider(self, spider):
        pass

    def _save_progress(self):
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump({'completed_urls': list(self.completed_urls)}, f, indent=4)
        except IOError as e:
            logging.error(f"Could not save progress file: {e}")

    def process_item(self, item, spider):
        url = item['source_url']
        if url in self.completed_urls:
            raise DropItem(f"URL already scraped: {url}")

        safe_filename = re.sub(r'[\\/*?:"<>|]', "_", url) + ".txt"
        filepath = self.data_folder / safe_filename

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(url + '\n')
                f.write(item['content'])
            
            self.completed_urls.add(url)
            self._save_progress()
        except IOError as e:
            logging.error(f"Could not write file {filepath}. Reason: {e}")
        
        return item

class DomainRestrictedSpider(CrawlSpider):
    name = "domain_scraper"

    def __init__(self, *args, **kwargs):
        super(DomainRestrictedSpider, self).__init__(*args, **kwargs)
        
        self.queue = kwargs.get('queue')
        self.start_urls = [kwargs.get('start_url')]
        self.output_path = kwargs.get('output_path')
        
        extracted_domain = tldextract.extract(self.start_urls[0])
        self.allowed_domains = [f"{extracted_domain.domain}.{extracted_domain.suffix}"]

        self.completed_urls = set()
        self.data_folder = None

        self.exclusion_pattern = kwargs.get('exclusion_pattern')
        self.use_regex = kwargs.get('use_regex')
        self.use_scrapling_engine = kwargs.get('use_scrapling_engine', False)
        self.scrapling_mode = kwargs.get('scrapling_mode', 'Balanced (Default)')
        
        self.compiled_regex = None
        if self.exclusion_pattern:
            try:
                if self.use_regex: pattern = self.exclusion_pattern
                else:
                    words = [re.escape(w.strip()) for w in re.split(r'[,\n]+', self.exclusion_pattern) if w.strip()]
                    pattern = r'\b(' + '|'.join(words) + r')\b' if words else None
                if pattern: self.compiled_regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                logging.error(f"Regex Error: Invalid pattern. {e}")
        
        DomainRestrictedSpider.rules = (
            Rule(
                LinkExtractor(allow_domains=self.allowed_domains), 
                callback='parse_page', 
                follow=True,
                process_links=self.filter_links
            ),
        )
        super(DomainRestrictedSpider, self)._compile_rules()

    def filter_links(self, links):
        return [link for link in links if link.url not in self.completed_urls]

    def parse_start_url(self, response):
        return self.parse_page(response)

    def parse_page(self, response):
        if response.url in self.completed_urls:
            return
            
        logging.info(f"Processing: {response.url}")
        
        if self.use_scrapling_engine:
            favor_recall = self.scrapling_mode == 'Capture More Content (Recall)'
            favor_precision = self.scrapling_mode == 'Capture Cleaner Content (Precision)'
            
            text = trafilatura.extract(
                response.body, 
                include_comments=False, 
                include_tables=False, 
                favor_recall=favor_recall,
                favor_precision=favor_precision
            )
            if text:
                yield ScrapedDataItem(source_url=response.url, content=text)
        else:
            soup = BeautifulSoup(response.text, 'lxml')
            for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                element.decompose()
            text = soup.get_text(separator='\n', strip=True)
            if self.compiled_regex: 
                text = self.compiled_regex.sub('', text)
            text = re.sub(r'[^a-zA-Z0-9 \n]', '', text)
            if text: 
                yield ScrapedDataItem(source_url=response.url, content=text)

# =============================================================================
# 2. MULTIPROCESSING WORKER FUNCTIONS
# =============================================================================

def run_scrapy_process(queue, config):
    try:
        # Step 1: Set up our QueueHandler to receive all log messages.
        setup_worker_logging(queue)
        
        log_level_str = config.get('log_level', 'INFO')
        log_level = getattr(logging, log_level_str.upper(), logging.INFO)
        
        # Step 2: Configure our own logger level
        logging.getLogger().setLevel(log_level)
        
        # Step 3: Configure third-party loggers
        configure_third_party_loggers(log_level_str)
        
        # Step 4: Implement the conditional Scrapy logging.
        scrapy_log_level = 'CRITICAL'
        if log_level_str == 'DEBUG':
            scrapy_log_level = 'DEBUG'
            logging.debug("Scrapy verbose logging enabled.")

        custom_settings = {
            "LOG_LEVEL": scrapy_log_level,
            "ITEM_PIPELINES": {'__main__.FilePipeline': 1},
            "ROBOTSTXT_OBEY": config.get('obey_robots', False),
            "DOWNLOAD_DELAY": config.get('download_delay', 0),
            "RETRY_ENABLED": True,
            "RETRY_TIMES": 5,
            "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
            "DOWNLOADER_MIDDLEWARES": {
                '__main__.RotateUserAgentAndProxyMiddleware': 543,
            },
        }

        process = CrawlerProcess(settings=custom_settings, install_root_handler=False)
        process.crawl(DomainRestrictedSpider, **config['spider_kwargs'], queue=queue)
        
        process.start()

    except Exception:
        logging.error(traceback.format_exc())
    finally:
        queue.put(('finished', 'scraping'))

def run_packaging_process(queue, domain_folder_name, output_path, word_limit, word_limit_enabled, log_level):
    try:
        setup_worker_logging(queue)
        log_level_obj = getattr(logging, log_level.upper(), logging.INFO)
        logging.getLogger().setLevel(log_level_obj)
        
        # Configure third-party loggers for packaging process
        configure_third_party_loggers(log_level)
        
        logging.info(f"Packaging results for {domain_folder_name}...")
        
        domain_folder = Path(output_path) / domain_folder_name
        output_folder = Path(output_path)
        
        txt_files = sorted(list(domain_folder.glob('*.txt')))
        if not txt_files:
            logging.warning(f"No text files found in {domain_folder_name} to process.")
            if domain_folder.exists():
                shutil.rmtree(domain_folder)
                logging.info(f"Cleaned up empty folder: {domain_folder_name}")
            return

        word_limit = word_limit if word_limit_enabled else float('inf')
        file_counter = 1
        current_word_count = 0
        story = []
        
        styles = getSampleStyleSheet()
        styles['h1'].fontSize = 16
        styles['h1'].leading = 20
        
        for txt_file in txt_files:
            with open(txt_file, 'r', encoding='utf-8') as f:
                source_url = f.readline().strip()
                content_str = f.read()

            content_str_escaped = html.escape(content_str).replace('\n', '<br/>')
            flowable = Paragraph(content_str_escaped, styles['BodyText'])
            item_word_count = len(content_str.split())

            if story and (current_word_count + item_word_count > word_limit):
                pdf_path = output_folder / f"{domain_folder_name}-{file_counter}.pdf"
                SimpleDocTemplate(str(pdf_path)).build(story)
                logging.info(f"SUCCESS: Created PDF: {pdf_path}")
                file_counter += 1
                story = []
                current_word_count = 0
            
            if not story:
                 story.extend([Paragraph(f"Web Scraper Pro Report: {domain_folder_name}", styles['h1']), Spacer(1, 0.25 * inch)])
            
            story.extend([Paragraph(source_url, styles['BodyText']), flowable, Spacer(1, 0.25 * inch)])
            current_word_count += item_word_count

        if story:
            filename = f"{domain_folder_name}.pdf" if file_counter == 1 else f"{domain_folder_name}-{file_counter}.pdf"
            pdf_path = output_folder / filename
            SimpleDocTemplate(str(pdf_path)).build(story)
            logging.info(f"SUCCESS: Created PDF: {pdf_path}")

        zip_filename = output_folder / f"{domain_folder_name}_scraped_content.zip"
        progress_file = domain_folder / f"progress_{domain_folder_name}.json"
        
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            robots_file = domain_folder / "robots.txt"
            if robots_file.exists():
                zipf.write(robots_file, arcname=robots_file.name)
            for file in txt_files:
                if file.exists():
                    zipf.write(file, arcname=file.name)
            if progress_file.exists():
                zipf.write(progress_file, arcname=progress_file.name)
        logging.info(f"SUCCESS: Created ZIP archive: {zip_filename}")

        shutil.rmtree(domain_folder)
        logging.info(f"SUCCESS: Cleaned up temporary folder: {domain_folder_name}")

    except Exception as e:
        logging.error(f"Packaging Error: {e}\n{traceback.format_exc()}")
    finally:
        queue.put(('finished', 'packaging'))

# =============================================================================
# 3. MAIN GUI APPLICATION
# =============================================================================

SETTINGS_FILE = "settings.json"

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Web Scraper Pro")
        self.root.geometry("800x860")
        
        self.current_process = None
        self.current_process_type = None
        self.is_running_queue = False
        self.current_task_index = -1
        self.tasks = []
        
        self.queue = Queue()

        self.setup_ui()
        self.load_settings()
        self.setup_logging()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.process_queue)

    def setup_logging(self):
        """Configures the root logger to send all logs to the GUI's queue with third-party control."""
        setup_main_logging(self.queue, self.log_level_var.get())

    def setup_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True)

        entry_group = ttk.LabelFrame(main_frame, text="Entry")
        entry_group.pack(fill=tk.X, padx=10, pady=5)
        entry_frame = ttk.Frame(entry_group)
        entry_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(entry_frame, text="Enter URL:").pack(side=tk.LEFT, padx=(0, 5))
        self.url_input = ttk.Entry(entry_frame)
        self.url_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.add_url_button = ttk.Button(entry_frame, text="Add URL", command=self.add_url)
        self.add_url_button.pack(side=tk.LEFT, padx=(5, 0))

        url_group = ttk.LabelFrame(main_frame, text="Target URLs")
        url_group.pack(fill=tk.X, padx=10, pady=5)
        
        columns = ('status', 'url')
        self.url_treeview = ttk.Treeview(url_group, columns=columns, show='headings', height=6, selectmode='browse')
        self.url_treeview.heading('status', text='Status')
        self.url_treeview.heading('url', text='URL')
        self.url_treeview.column('status', width=120, stretch=tk.NO, anchor=tk.W)
        self.url_treeview.column('url', width=500)
        self.url_treeview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.url_treeview.bind('<<TreeviewSelect>>', self.on_task_select)

        self.url_treeview.tag_configure('Done', background='#d9ead3')
        self.url_treeview.tag_configure('Failed', background='#f4cccc')
        self.url_treeview.tag_configure('Stopped', background='#e6b8af')
        self.url_treeview.tag_configure('Scraping', background='#fff2cc')
        self.url_treeview.tag_configure('Packaging', background='#fff2cc')
        self.url_treeview.tag_configure('Pending', background='white')

        url_scroll = ttk.Scrollbar(url_group, orient=tk.VERTICAL, command=self.url_treeview.yview)
        url_scroll.pack(side=tk.LEFT, fill=tk.Y, pady=5)
        self.url_treeview.config(yscrollcommand=url_scroll.set)

        url_controls_frame = ttk.Frame(url_group)
        url_controls_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(5, 5), pady=5)
        ttk.Button(url_controls_frame, text="Up", command=self.move_item_up).pack(fill=tk.X, pady=2)
        ttk.Button(url_controls_frame, text="Down", command=self.move_item_down).pack(fill=tk.X, pady=2)
        ttk.Button(url_controls_frame, text="Remove", command=self.remove_url).pack(fill=tk.X, pady=2)
        ttk.Button(url_controls_frame, text="Reset", command=self.reset_task).pack(fill=tk.X, pady=(10, 2))
        self.pdf_zip_button = ttk.Button(url_controls_frame, text="PDF & ZIP", command=self.manual_package_selected_result, state=tk.DISABLED)
        self.pdf_zip_button.pack(fill=tk.X, pady=2)

        control_group = ttk.LabelFrame(main_frame, text="Process Controls")
        control_group.pack(fill=tk.X, padx=10, pady=5)
        self.start_stop_button = ttk.Button(control_group, text="> Start Scraping", command=self.toggle_scraping)
        self.start_stop_button.pack(fill=tk.X, padx=5, pady=5)
        
        # --- Configuration Section ---
        self.config_group = ttk.LabelFrame(main_frame, text="Configuration")
        self.config_group.pack(fill=tk.X, padx=10, pady=5)
        
        top_config_frame = ttk.Frame(self.config_group)
        top_config_frame.pack(fill=tk.X, padx=5, pady=5)

        self.output_path_var = tk.StringVar()
        ttk.Label(top_config_frame, text="Output:").grid(row=0, column=0, sticky=tk.W, pady=(0, 5))
        self.output_entry = ttk.Entry(top_config_frame, textvariable=self.output_path_var, state='readonly', width=80)
        self.output_entry.grid(row=0, column=1, sticky='ew', pady=(0, 5), padx=5)
        self.select_output_button = ttk.Button(top_config_frame, text="Select", command=self.select_output_folder)
        self.select_output_button.grid(row=0, column=2, sticky=tk.W, pady=(0, 5))
        
        self.log_level_var = tk.StringVar(value="INFO")
        log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        ttk.Label(top_config_frame, text="Log Level:").grid(row=1, column=0, sticky=tk.W)
        self.log_level_combo = ttk.Combobox(top_config_frame, textvariable=self.log_level_var, values=log_levels, state='readonly')
        self.log_level_combo.grid(row=1, column=1, sticky=tk.W, padx=5)
        self.log_level_combo.bind('<<ComboboxSelected>>', self.update_log_level)
        
        top_config_frame.grid_columnconfigure(1, weight=1)

        # --- Populate Configuration Options ---
        config_options_frame = ttk.Frame(self.config_group)
        config_options_frame.pack(fill=tk.X, padx=5, pady=5)
        
        self.use_scrapling_var = tk.BooleanVar(value=True)
        self.scrapling_checkbox = ttk.Checkbutton(config_options_frame, text="Use Scrapling Engine (Intelligent Extraction)", variable=self.use_scrapling_var, command=self.toggle_scrapling_mode)
        self.scrapling_checkbox.grid(row=0, column=0, sticky=tk.W, pady=(0, 5))

        self.scrapling_mode_var = tk.StringVar(value='Capture More Content (Recall)')
        scrapling_options = ['Balanced (Default)', 'Capture More Content (Recall)', 'Capture Cleaner Content (Precision)']
        self.scrapling_mode_combo = ttk.Combobox(config_options_frame, textvariable=self.scrapling_mode_var, values=scrapling_options, state='readonly')
        self.scrapling_mode_combo.grid(row=0, column=1, sticky=tk.W, pady=(0, 5), padx=5)

        self.pdf_limit_var = tk.BooleanVar(value=True)
        self.pdf_limit_checkbox = ttk.Checkbutton(config_options_frame, text="Words per PDF:", variable=self.pdf_limit_var)
        self.pdf_limit_checkbox.grid(row=1, column=0, sticky=tk.W)
        self.word_limit_spinbox = ttk.Spinbox(config_options_frame, from_=1000, to=5000000, increment=1000, width=10)
        self.word_limit_spinbox.set(495000)
        self.word_limit_spinbox.grid(row=1, column=1, sticky=tk.W)
        self.delay_var = tk.BooleanVar(value=False)
        self.delay_checkbox = ttk.Checkbutton(config_options_frame, text="Enable Request Delay (seconds):", variable=self.delay_var)
        self.delay_checkbox.grid(row=2, column=0, sticky=tk.W)
        self.delay_spinbox = ttk.Spinbox(config_options_frame, from_=0.1, to=60.0, increment=0.1, width=10)
        self.delay_spinbox.set(2.0)
        self.delay_spinbox.grid(row=2, column=1, sticky=tk.W)
        
        self.use_regex_var = tk.BooleanVar(value=False)
        self.use_regex_checkbox = ttk.Checkbutton(config_options_frame, text="Use Regex for Exclusions", variable=self.use_regex_var)
        self.use_regex_checkbox.grid(row=3, column=0, sticky=tk.W)
        self.obey_robots_var = tk.BooleanVar(value=False)
        self.obey_robots_checkbox = ttk.Checkbutton(config_options_frame, text="Obey robots.txt rules", variable=self.obey_robots_var)
        self.obey_robots_checkbox.grid(row=3, column=1, sticky=tk.W)
        
        ttk.Label(config_options_frame, text="Exclusion List (comma or newline separated):").grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10,0))
        self.exclusion_input = ScrolledText(config_options_frame, height=4, width=80)
        self.exclusion_input.grid(row=5, column=0, columnspan=2, sticky="ew")
        config_options_frame.grid_columnconfigure(1, weight=1)

        log_group = ttk.LabelFrame(main_frame, text="Status Log")
        log_group.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_console = ScrolledText(log_group, height=10, state=tk.DISABLED)
        self.log_console.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.status_var = tk.StringVar(value="Ready. Add URLs to begin.")
        self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.toggle_scrapling_mode()

    def update_log_level(self, event=None):
        """Update log level for all loggers including third-party libraries."""
        level_str = self.log_level_var.get().upper()
        log_level = getattr(logging, level_str, logging.INFO)
        logging.getLogger().setLevel(log_level)
        
        # Configure third-party loggers
        configure_third_party_loggers(level_str)
        
        logging.info(f"Log level changed to {level_str}")

        if log_level <= logging.DEBUG:
            log_levels_report = ["--- Logger Levels in GUI Process ---"]
            root_level_name = logging.getLevelName(logging.getLogger().getEffectiveLevel())
            log_levels_report.append(f"  - root: {root_level_name}")
            for name in sorted(logging.Logger.manager.loggerDict.keys()):
                logger = logging.getLogger(name)
                level_name = logging.getLevelName(logger.getEffectiveLevel())
                log_levels_report.append(f"  - {name}: {level_name}")
            logging.debug('\n'.join(log_levels_report))

    def _set_children_state(self, parent_widget, state):
        for child in parent_widget.winfo_children():
            try:
                if isinstance(child, ttk.Combobox) or isinstance(child, ttk.Entry) and child.cget('state') == 'readonly':
                     child.config(state='readonly' if state == tk.NORMAL else tk.DISABLED)
                else:
                    child.config(state=state)
            except tk.TclError:
                self._set_children_state(child, state)

    def select_output_folder(self):
        folder_path = filedialog.askdirectory()
        if folder_path:
            self.output_path_var.set(folder_path)

    def toggle_scrapling_mode(self):
        if self.use_scrapling_var.get():
            self.scrapling_mode_combo.config(state='readonly')
        else:
            self.scrapling_mode_combo.config(state=tk.DISABLED)

    def add_url(self):
        url = self.url_input.get().strip()
        if not url: return
        try:
            result = urlparse(url)
            if not all([result.scheme, result.netloc]): raise ValueError
        except ValueError:
            messagebox.showerror("Invalid URL", "Please enter a valid URL (e.g., http://example.com).")
            return
        if url in [t['url'] for t in self.tasks]:
            messagebox.showinfo("Duplicate URL", "This URL is already in the list.")
            return
        threading.Thread(target=self.validate_and_add_url_thread, args=(url,), daemon=True).start()

    def validate_and_add_url_thread(self, url):
        self.root.after(0, lambda: self.add_url_button.config(state=tk.DISABLED))
        try:
            logging.info(f"Validating URL: {url}...")
            headers = {'User-Agent': choice(USER_AGENTS)}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                logging.error(f"URL validation failed. Status code: {response.status_code}")
                messagebox.showerror("Validation Failed", f"Could not reach URL. Server responded with status code {response.status_code}.")
                return
            logging.info(f"URL is live (Status Code: {response.status_code}).")
            robots_url = urljoin(url, "/robots.txt")
            logging.info(f"Checking for robots.txt at: {robots_url}...")
            robots_response = requests.get(robots_url, headers=headers, timeout=10)
            if robots_response.status_code == 200:
                extracted = tldextract.extract(url)
                domain_name = extracted.domain or extracted.subdomain
                output_dir = Path(self.output_path_var.get()) / domain_name
                output_dir.mkdir(exist_ok=True, parents=True)
                with open(output_dir / "robots.txt", 'w', encoding='utf-8') as f: f.write(robots_response.text)
                logging.info(f"robots.txt found and saved to '{output_dir.name}' folder.")
            else:
                logging.warning(f"robots.txt not found (Status Code: {robots_response.status_code}).")
            def update_ui():
                self.tasks.append({'url': url, 'status': 'Pending'})
                self.update_task_list()
                self.url_input.delete(0, tk.END)
                logging.info(f"Added URL to the queue: {url}")
            self.root.after(0, update_ui)
        except requests.exceptions.RequestException as e:
            logging.error(f"Could not connect to the URL. {e}")
            messagebox.showerror("Connection Error", f"An error occurred while trying to connect to the URL:\n\n{e}")
        finally:
            self.root.after(0, lambda: self.add_url_button.config(state=tk.NORMAL))

    def remove_url(self):
        selected_items = self.url_treeview.selection()
        if not selected_items: return
        index_to_remove = int(selected_items[0])
        del self.tasks[index_to_remove]
        self.update_task_list()
        self.on_task_select()

    def move_item_up(self):
        selected_items = self.url_treeview.selection()
        if not selected_items: return
        idx = int(selected_items[0])
        if idx > 0:
            self.tasks.insert(idx - 1, self.tasks.pop(idx))
            self.update_task_list(select_index=idx - 1)

    def move_item_down(self):
        selected_items = self.url_treeview.selection()
        if not selected_items: return
        idx = int(selected_items[0])
        if idx < len(self.tasks) - 1:
            self.tasks.insert(idx + 1, self.tasks.pop(idx))
            self.update_task_list(select_index=idx + 1)

    def reset_task(self):
        selected_items = self.url_treeview.selection()
        if not selected_items: return
        index = int(selected_items[0])
        if 0 <= index < len(self.tasks):
            self.tasks[index]['status'] = 'Pending'
            self.update_task_list(select_index=index)
            self.on_task_select()

    def update_task_list(self, select_index=None):
        selected_item = self.url_treeview.selection()
        selected_iid = selected_item[0] if selected_item else None
        self.url_treeview.delete(*self.url_treeview.get_children())
        for i, task in enumerate(self.tasks):
            status = task['status']
            tag_name = status.split('...')[0]
            if 'ing' in tag_name: tag_name = 'Scraping'
            self.url_treeview.insert('', tk.END, iid=i, values=(status, task['url']), tags=(tag_name,))
        if select_index is not None and select_index < len(self.tasks):
            self.url_treeview.selection_set(str(select_index))
            self.url_treeview.focus(str(select_index))
        elif selected_iid and int(selected_iid) < len(self.tasks):
             self.url_treeview.selection_set(selected_iid)
             self.url_treeview.focus(selected_iid)

    def on_task_select(self, event=None):
        selected_items = self.url_treeview.selection()
        if not selected_items:
            self.pdf_zip_button.config(state=tk.DISABLED)
            return
        selected_index = int(selected_items[0])
        task = self.tasks[selected_index]
        extracted = tldextract.extract(task['url'])
        domain_name = extracted.domain or extracted.subdomain
        data_folder = Path(self.output_path_var.get()) / domain_name
        if data_folder.exists() and any(data_folder.iterdir()):
            self.pdf_zip_button.config(state=tk.NORMAL)
        else:
            self.pdf_zip_button.config(state=tk.DISABLED)

    def toggle_scraping(self):
        if self.current_process and self.current_process.is_alive(): self.stop_scraping()
        else: self.start_scraping()

    def start_scraping(self):
        if not self.output_path_var.get() or not Path(self.output_path_var.get()).is_dir():
            messagebox.showerror("Invalid Output Path", "Please select a valid output folder before starting.")
            return
        if not any(t['status'] in ['Pending', 'Stopped', 'Failed'] for t in self.tasks):
            messagebox.showinfo("No Tasks", "All URLs have been processed. Add new URLs to start.")
            return
        self.is_running_queue = True
        self.set_controls_enabled(False)
        self.start_stop_button.config(text="Stop Scraping")
        logging.info("--- Starting scraping process... ---")
        self.process_next_url()

    def stop_scraping(self):
        self.is_running_queue = False
        if self.current_process and self.current_process.is_alive():
            logging.info("Stop signal sent. Terminating current job...")
            if self.current_task_index != -1:
                self.tasks[self.current_task_index]['status'] = "Stopped"
                self.update_task_list(select_index=self.current_task_index)
            self.current_process.terminate()
            self.current_process.join(timeout=1)
            self.on_process_stopped()

    def on_process_stopped(self):
        logging.info("Process stopped by user.")
        self.status_var.set("Paused. You can process the results or start again.")
        self.set_controls_enabled(True)
        self.start_stop_button.config(text="> Start Scraping")
        self.on_task_select()

    def process_next_url(self):
        next_task_index = -1
        for i, task in enumerate(self.tasks):
            if task['status'] in ['Pending', 'Stopped', 'Failed']:
                next_task_index = i
                break
        if next_task_index == -1:
            self.all_tasks_complete()
            return
        self.current_task_index = next_task_index
        task = self.tasks[self.current_task_index]
        url = task['url']
        task['status'] = 'Scraping...'
        self.update_task_list(select_index=self.current_task_index)
        logging.info(f"--- Starting job for: {url} ---")
        config = {
            'download_delay': float(self.delay_spinbox.get()) if self.delay_var.get() else 0,
            'obey_robots': self.obey_robots_var.get(),
            'log_level': self.log_level_var.get(),
            'spider_kwargs': {
                'start_url': url,
                'output_path': self.output_path_var.get(),
                'exclusion_pattern': self.exclusion_input.get(1.0, tk.END),
                'use_regex': self.use_regex_var.get(),
                'use_scrapling_engine': self.use_scrapling_var.get(),
                'scrapling_mode': self.scrapling_mode_var.get(),
            }
        }
        self.current_process_type = 'scraping'
        self.current_process = Process(target=run_scrapy_process, args=(self.queue, config))
        self.current_process.daemon = True
        self.current_process.start()

    def start_packaging(self, selected_task_index):
        task = self.tasks[selected_task_index]
        task['status'] = 'Packaging...'
        self.update_task_list(select_index=selected_task_index)
        extracted = tldextract.extract(task['url'])
        domain_name = extracted.domain or extracted.subdomain
        self.current_process_type = 'packaging'
        self.current_process = Process(target=run_packaging_process, args=(
            self.queue, domain_name, self.output_path_var.get(),
            int(self.word_limit_spinbox.get()), self.pdf_limit_var.get(),
            self.log_level_var.get()
        ))
        self.current_process.daemon = True
        self.current_process.start()

    def manual_package_selected_result(self):
        selected_items = self.url_treeview.selection()
        if not selected_items:
            messagebox.showinfo("No Selection", "Please select a task to package.")
            return
        selected_index = int(selected_items[0])
        self.current_task_index = selected_index
        self.set_controls_enabled(False)
        self.start_packaging(selected_index)

    def process_queue(self):
        try:
            while not self.queue.empty():
                message_type, data = self.queue.get_nowait()
                if message_type == 'log':
                    self.log_message(data)
                    # Check for specific error message to update task status
                    if "ERROR:" in data or "Error:" in data and self.current_task_index != -1:
                        if self.tasks[self.current_task_index]['status'] not in ['Done', 'Stopped']:
                           self.tasks[self.current_task_index]['status'] = 'Failed'
                           self.update_task_list(select_index=self.current_task_index)
                elif message_type == 'finished':
                    self.handle_job_finished(process_type=data)
        finally:
            self.root.after(100, self.process_queue)

    def handle_job_finished(self, process_type):
        if self.current_process_type == 'packaging' and not self.is_running_queue:
            if self.tasks[self.current_task_index]['status'] != 'Failed':
                self.tasks[self.current_task_index]['status'] = 'Done'
            self.update_task_list(select_index=self.current_task_index)
            self.all_tasks_complete()
            return
        if not self.is_running_queue: return
        task = self.tasks[self.current_task_index]
        if process_type == 'scraping':
            self.start_packaging(self.current_task_index)
        elif process_type == 'packaging':
            if task['status'] != 'Failed': task['status'] = 'Done'
            self.update_task_list(select_index=self.current_task_index)
            self.process_next_url()

    def all_tasks_complete(self):
        logging.info("\n--- All jobs complete. ---")
        self.status_var.set("Process finished.")
        self.set_controls_enabled(True)
        self.is_running_queue = False
        self.current_process = None
        self.start_stop_button.config(text="> Start Scraping")

    def set_controls_enabled(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.add_url_button.config(state=state)
        self.pdf_zip_button.config(state=tk.DISABLED)
        for child in self.url_treeview.master.winfo_children():
            if isinstance(child, ttk.Frame):
                for button in child.winfo_children():
                    if isinstance(button, ttk.Button) and button != self.pdf_zip_button:
                        button.config(state=state)
        self.start_stop_button.config(state=tk.NORMAL) 
        self._set_children_state(self.config_group, state)
        if enabled:
            self.on_task_select()
            self.toggle_scrapling_mode()

    def log_message(self, message):
        self.log_console.config(state=tk.NORMAL)
        self.log_console.insert(tk.END, message + '\n')
        self.log_console.see(tk.END)
        self.log_console.config(state=tk.DISABLED)
        self.status_var.set(message.split('\n')[-1])

    def load_settings(self):
        default_output_path = str(Path.home() / "Downloads")
        if not os.path.exists(SETTINGS_FILE):
            self.output_path_var.set(default_output_path)
            return
        try:
            with open(SETTINGS_FILE, 'r') as f: settings = json.load(f)
            self.output_path_var.set(settings.get('output_path', default_output_path))
            self.log_level_var.set(settings.get('log_level', 'INFO'))
            self.pdf_limit_var.set(settings.get('word_limit_enabled', True))
            self.word_limit_spinbox.set(settings.get('word_limit', 495000))
            self.delay_var.set(settings.get('delay_enabled', False))
            self.delay_spinbox.set(settings.get('delay_seconds', 2.0))
            self.exclusion_input.delete(1.0, tk.END)
            self.exclusion_input.insert(1.0, settings.get('exclusion_list', ''))
            self.use_regex_var.set(settings.get('use_regex', False))
            self.obey_robots_var.set(settings.get('obey_robots', False))
            self.use_scrapling_var.set(settings.get('use_scrapling_engine', True))
            self.scrapling_mode_var.set(settings.get('scrapling_mode', 'Capture More Content (Recall)'))
            self.tasks = settings.get('tasks', [])
            self.update_task_list()
        except (json.JSONDecodeError, IOError) as e:
            print(f"WARNING: Could not load settings file. Reason: {e}")
            self.output_path_var.set(default_output_path)
        finally:
            self.toggle_scrapling_mode()

    def save_settings(self):
        settings = {
            'output_path': self.output_path_var.get(),
            'log_level': self.log_level_var.get(),
            'word_limit_enabled': self.pdf_limit_var.get(),
            'word_limit': int(self.word_limit_spinbox.get()),
            'delay_enabled': self.delay_var.get(),
            'delay_seconds': float(self.delay_spinbox.get()),
            'exclusion_list': self.exclusion_input.get(1.0, tk.END).strip(),
            'use_regex': self.use_regex_var.get(),
            'obey_robots': self.obey_robots_var.get(),
            'use_scrapling_engine': self.use_scrapling_var.get(),
            'scrapling_mode': self.scrapling_mode_var.get(),
            'tasks': self.tasks
        }
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=4)
        except IOError as e:
            logging.error(f"Could not save settings. Reason: {e}")

    def on_close(self):
        if self.current_process and self.current_process.is_alive():
            self.stop_scraping()
        self.save_settings()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == '__main__':
    freeze_support()
    app = MainWindow()
    app.run()