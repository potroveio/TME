import asyncio
import imapclient
import io
import json
import logging
import os
import datetime
import pyppeteer
import requests
import time
import re

import settings as s

try:
    from urllib import quote_plus
except:
    from urllib.parse import quote_plus


logger = logging.getLogger("job_checker")
logger.setLevel(logging.INFO)
# create file handler which logs even debug messages
fh = logging.FileHandler('errors.log', mode='a')
fh.setLevel(logging.INFO)
# create console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
# create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
# add the handlers to the logger
logger.addHandler(fh)
logger.addHandler(ch)



class Worker:

    @staticmethod
    def human_readable_dict(data):
        text = "New Job Listing:\n\n"
        if isinstance(data, dict):
            for k, v in data.items():
                text += "{}: {}".format(k, v)
        else:
            text = str(data)
        return text
        
    @staticmethod
    def send_message(text):
        for chat in s.CHAT_ID:
            try:
                base_url = "https://api.telegram.org/bot{}/sendMessage?chat_id={}&text={}".format(
                    s.BOT_TOKEN, chat, quote_plus(Worker.human_readable_dict(text))) 
                requests.get(base_url)
            except Exception as e:
                logger.exception(e)

    async def save_page(self):
        timestr = time.strftime("%Y%m%d-%H%M%S")
        save_path = '.', 'evidences'

        if not os.path.exists(os.path.join(*save_path)):
            os.makedirs(os.path.join(*save_path))

        with io.open(os.path.join(*save_path, (timestr + ".txt")), "w", encoding="utf-8") as f:
            f.write(await self.page.content())

        await self.page.screenshot({'path': os.path.join(*save_path, (timestr + ".png"))})

    async def confirm_task(self, task_id, translation_id):

        res = await self.get_json_from_page(f'https://www.tm-stream.com/2_0/Handlers/TransPortal.asmx/AllocateTranslation?idTask={task_id}&idTranslation={translation_id}&isPreAllocate=1')

        if res.get('IsSuccess'):
            logger.info('Confirmed task')
            Worker.send_message('Task confirmed')
        else:
            logger.info('Task not confirmed')
            Worker.send_message('Task not confirmed')


    async def check_mail_notifications(self):
        logger.info("Login on Gmail...")
        server = imapclient.IMAPClient('imap.gmail.com', use_uid=True)
        server.login(s.GMAIL_EMAIL, s.GMAIL_PASSWORD)
        select_info = server.select_folder('INBOX')
        messages = server.search(['OR', 'SUBJECT', 'New Job Alert', 
                                            'SUBJECT', 'New revision job coming up',
                                            u'UNSEEN'])
        logger.info("%d messages from TranslateMedia\n" % len(messages))

        # Scraping task links from new e-mails found
        for mail_id, data in server.fetch(messages,['ENVELOPE','BODY[TEXT]']).items():
            envelope, body = data[b'ENVELOPE'], data[b'BODY[TEXT]']
            logger.info('id #%s Received on %s - Subject: "%s"\n' % (mail_id, envelope.date, envelope.subject.decode()))
            await self.page.goto('https://www.tm-stream.com/2_0/TranslatorPortal/defaultng.aspx?view=jobBoard')
            await self.page.waitFor(2000)
            Worker.send_message("TranslateMedia e-mail task found. Dont forget to CTRL S")
            await self.save_page()
            await self.checkjobs()

        server.logout()

    async def check_ongoing_jobs(self):
        await self.page.goto('https://www.tm-stream.com/2_0/TranslatorPortal/defaultng.aspx#/jobBoard')
        await self.page.waitFor(4000)
        
        job_confirm = await self.page.querySelector('.tm-tp-ongoing-jobs button')

        if job_confirm:
            await self.save_page()
            try:
                await job_confirm.click()
                Worker.send_message('TME task details opened. Need to see new css selector to confirm!')
                await self.confirm_task()
            except:
                logger.info('css selector button.btn found, but not clickable')
                Worker.send_message('a task was found, but couldnt be clicked to open and confirm')

        else:
            logger.info('No jobs found with css selector button.btn')

    async def prepare(self):
        self.browser = await pyppeteer.launch(
            # headless=False,
            # executablePath=r'.\GoogleChromePortable\GoogleChromePortable.exe',
            defaultViewport=None
        )
        self.page = await self.browser.newPage()

    async def waitFor(self, function, *args):
        await function(*args)
        await self.page.waitFor(400)

    async def get_json_from_page(self, url):
        await self.page.goto(url)
        content = await (await self.page.xpath(".//html"))[0].getProperty('textContent')
        json_content = json.loads(await content.jsonValue())
        return json_content

    async def check_available_jobs(self):

        json_content = await self.get_json_from_page('https://www.tm-stream.com/2_0/Handlers/TransPortal.asmx/GetAvailableJobs')
        available_jobs = json_content['FutureAllocatedRevisionJobs']

        if available_jobs:

            job = available_jobs[0]
            task_id = job['idTask']
            translation_id = job['idTranslation']
            self.confirm_task(task_id, translation_id)

            job_info = Worker.get_job_info(job)

            logger.info("{} New Jobs Available in India".format(len(available_jobs)))
            Worker.send_message("TranslateMedia task found with Indian Method. Saving source to examine. \n" + job_info)

            await self.page.goto('https://www.tm-stream.com/2_0/TranslatorPortal/defaultng.aspx#/jobBoard')
            await self.save_page()
            time.sleep(1)

    async def login(self):
        await self.page.goto('https://www.tm-stream.com/2_0/login/login.html#/Login')
        await self.page.waitForSelector('#form-username')

        await self.waitFor(self.page.type, '#form-username', s.EMAIL)
        await self.waitFor(self.page.type, '#form-password', s.PASSWORD)
        await self.waitFor(self.page.click, 'button')

        await self.page.waitForNavigation()

    async def close(self):
        await self.browser.close()

    @staticmethod
    def get_job_info(job):
        fee_payable = job['FeePayable']
        dead_line = Worker.parse_time_to_native(job['scheduleCompleteTime'])
        return f'Fee Payable: ${fee_payable}, Dead line: {dead_line}'

    @staticmethod
    def parse_time_to_native(time):
        str_timestamp = re.search(r'\d+', time).group()
        timestamp = int(str_timestamp)
        date = datetime.datetime.fromtimestamp(timestamp/1000)
        return date.strftime('%Y-%m-%d %H:%M:%S')


async def main():
    worker = Worker()
    await worker.prepare()
    await worker.login()

    LOOP_DELAY = 10000 # in milliseconds

    while True:

        try:
            await worker.check_ongoing_jobs()
            await worker.check_available_jobs()
        except Exception as e:
            logger.exception(e)

        try:
            await worker.check_mail_notifications()
        except imapclient.imaplib.IMAP4.error as e:
            logger.warning('Cannot login in the email account')
        except Exception as e:
            logger.exception(e)

        await worker.page.waitFor(LOOP_DELAY)

    # when closing the browser
    await worker.close()


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())
