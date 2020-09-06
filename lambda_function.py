import gkeepapi
import json
import pickle
import os.path
import traceback
import io
import smtplib, ssl
import threading
import time
from queue import Queue
from contextlib import redirect_stdout
from datetime import timedelta, timezone
from googleapiclient.discovery import build
from google.oauth2 import service_account


def lambda_handler(event, context):
    """Called by AWS Lambda when triggered. Calls main, and returns the traceback
    if there is an exception.
    """
    trace = 'Success!'
    try:
        main()
    except Exception as e:
        trace = traceback.format_exc()
        print(trace)
    return {
        'statusCode': 200,
        'body': json.dumps(trace)
    }


def getLastHeader(doc, headerNum):
    """Gets the text of the last header with the named style type of heading
    headerNum. Used to extract the most previously added dates.

    Args:
        doc (Google Docs Document): Google Docs Document to extract text from
        headerNum (int): Number of header

    Returns:
        String: A stripped version of the text which appears in the le
    """
    # Iterate through content in reverse. If a paragraph is found whose
    # nameStyleType matches the desired header, return its content.
    for content in reversed(doc['body']['content']):
        if 'paragraph' in content and \
        content['paragraph']['paragraphStyle']['namedStyleType'] == 'HEADING_' + headerNum:
            return content['paragraph']['elements'][0]['textRun']['content'].strip()


def getLastDate(doc):
    """Gets the last year, month, and day added to the document.

    Args:
        doc (Google Docs Document): Google Docs Document to extract text from

    Returns:
        lastYear (int): last year added to document
        lastMonth (int): last month added to document
        lastDay (int): last day added to document
    """
    # Last year is the text in the appropriate header as an int
    lastYear = int(getLastHeader(doc, config['YEAR_HEADER_NUM']))
    # Last month is the index that the text in the appropriate header occurs in
    # in the months list
    lastMonth = months.index(getLastHeader(doc, config['MONTH_HEADER_NUM']))
    # Last day is the numbers present in the appropriate header
    lastDay = int(''.join([char for char in getLastHeader(doc, config['DAY_HEADER_NUM']) if char.isdigit()]))
    return lastYear, lastMonth, lastDay


def getEndIndex(doc):
    """Gets the ending index of the last element of the document.

    Args:
        doc (Google Docs Document): Google Docs Document to extract text from

    Returns:
        int: endIndex of the last element of the document
    """
    return doc['body']['content'][-1]['endIndex']


def addText(text, endIndex, namedStyleType, requests):
    """Adds requests to add text at endIndex with the desired nameStyleType.
    Returns new endIndex.

    Args:
        text (String): text to add
        endIndex (int): Current endIndex of document. Location to add text to
        namedStyleType (String): desired namedStyleType of text
        requests (list): list of requests to append requests to

    Returns:
        int: new endIndex after adding text
    """
    # Finds new end index by incrementing endIndex by the length of the text
    # plus 1 for the automatically appended '\n'
    newEndIndex = endIndex + len(text) + 1
    # Appends requests to add text, and then request to change namedStyleType
    # as desired
    requests.extend([
        # Add text, including a '\n' at the previous line to create a new paragraph
        # Note that a '\n' will automatically be appended to the end of the text
        {
            'insertText': {
                'location': {
                    'index': endIndex - 1
                },
                'text': '\n' + text
            }
        },
        {
            'updateParagraphStyle': {
                'range': {
                    'startIndex': endIndex,
                    'endIndex': newEndIndex
                },
                'paragraphStyle': {
                    'namedStyleType': namedStyleType
                },
                'fields': 'namedStyleType'
            }
        }
    ])
    return newEndIndex


def ordinal(n):
    """Gets the ordinal string of any int from 1 to 100.

    Args:
        n (int): int to get ordinal of

    Returns:
        String: ordinal string of n
    """
    # Determines suffix based about n % 10
    suffix = ['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]
    # Updates suffix for special case
    if 11 <= n <= 13:
        suffix = 'th'
    return str(n) + suffix


def getTime(datetime):
    """Returns the time of a datetime object as a String in the traditional
    12-hour clock format.

    Args:
        datetime (datetime): datetime object to return time of

    Returns:
        String: time of datetime in 12-hour clock format
    """

    # Determine hour and suffix for 12-hour clock format based upon the value
    # of datetime.hour
    if datetime.hour == 0:
        hour = 12
        suffix = 'am'
    elif datetime.hour < 12:
        hour = datetime.hour
        suffix = 'am'
    elif datetime.hour == 12:
        hour = datetime.hour
        suffix = 'pm'
    else:
        hour = datetime.hour % 12
        suffix = 'pm'

    # If minute is only a single digit, prepend it with a 0, else keep it the same
    minute = str(datetime.minute) if datetime.minute >= 10 \
             else '0' + str(datetime.minute)

    return '{}:{}{}'.format(hour, minute, suffix)


def notesToGoogleDoc(notes):
    """Given a set of notes, add to the document in the desired format.

    Args:
        notes (list): list of keep.note objects
    """
    # Log in to Google Docs API using service account. It is necessary to login
    # this way, as opposed to the standard process of validating using a pickled
    # token, in order for login to work on AWS Lambda.
    SCOPES = ['https://www.googleapis.com/auth/documents']
    creds = service_account.Credentials.from_service_account_file(
        config['CREDENTIALS_FILE'], scopes=SCOPES)
    service = build('docs', 'v1', credentials=creds)

    # Retrieve the documents contents from the Docs service.
    DOCUMENT_ID = config['DOCUMENT_ID']
    doc = service.documents().get(documentId=DOCUMENT_ID).execute()

    # List used to match weekday numbers, with 0-based indexing, to the
    # appropriate String
    weekDays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                'Saturday', 'Sunday']
    # Last year, month, and day present in document
    lastYear, lastMonth, lastDay = None, None, None
    # endIndex of last entry in document
    endIndex = None
    # List of requests to batch
    requests = []

    for note in notes:
        # If endIndex and other variables have not been assigned (iff this is
        # the first note of the iterator) then assign the variables
        if not endIndex:
            lastYear, lastMonth, lastDay = getLastDate(doc)
            endIndex = getEndIndex(doc)

        # Time that note was created, adjusted by the appropriate timezone
        timeCreated = note.timestamps.created.astimezone(
            tz=timezone(timedelta(hours=config['TZ_ADJUSTMENT'])))

        # If lastYear is not equal to the year that note was created, add the
        # year to the document in the appropriate format. Update endIndex and
        # lastYear
        if timeCreated.year != lastYear:
            endIndex = addText(str(timeCreated.year), endIndex,
                               'HEADING_' + config['YEAR_HEADER_NUM'], requests)
            lastYear = timeCreated.year

        # If lastMonth is not equal to the month that note was created, add the
        # month to the document in the appropriate format. Update endIndex and
        # lastMonth
        if timeCreated.month != lastMonth:
            endIndex = addText(months[timeCreated.month], endIndex,
                               'HEADING_' + config['MONTH_HEADER_NUM'], requests)
            lastMonth = timeCreated.month

        # If lastDay is not equal to the day that note was created, add the
        # day to the document in the appropriate format. Update endIndex and
        # lastDay
        if timeCreated.day != lastDay:
            weekday = weekDays[timeCreated.weekday()]
            endIndex = addText(weekday + ' ' + ordinal(timeCreated.day),
                               endIndex, 'HEADING_' + config['DAY_HEADER_NUM'],
                               requests)
            lastDay = timeCreated.day

        # Add the title of the note, the time it was created, and the text of the
        # note each in the appropriate format
        endIndex = addText(note.title, endIndex,
                           'HEADING_' + config['TITLE_HEADER_NUM'], requests)
        endIndex = addText(getTime(timeCreated), endIndex,
                           'HEADING_' + config['TIME_HEADER_NUM'], requests)
        endIndex = addText(note.text, endIndex, 'NORMAL_TEXT', requests)

    # Execute a batch request to complete each request in order
    result = service.documents().batchUpdate(
        documentId=DOCUMENT_ID, body={'requests': requests}).execute()

def deleteNotes(notes, keep):
    """Deletes notes from Keep.

    Args:
        notes (list): list of notes to delete
        keep (Keep object): Keep object representing Keep account to remove notes
        from
    """
    for note in notes:
        note.delete()
    keep.sync()


def main():
    """Gets notes from Keep, adds them to the document in the desired format,
    and deletes the notes from Keep.
    """
    # contains both customizable settings and login info that shouldn't be
    # version controlled
    filename = './config.json'
    with open(filename, 'r') as jsonFile:
        global config
        config = json.load(jsonFile)

    # List of months to go from month number of appropriate String. Month numbers
    # use 1-based indexing, so a filler is added at index 0 to avoid off-by-one
    # errors
    global months
    months = ['FILLER', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']

    # Login to Keep
    keep = gkeepapi.Keep()
    success = keep.login(config['USERNAME'], config['PASSWORD'])

    # Gets all notes with the appropriate label and sort them based upon time
    # created
    notes = sorted(keep.find(labels=[keep.findLabel(config['KEEP_LABEL'])]),
                   key=lambda x: x.timestamps.created)

    # If any such notes are found, added them to the document and delete them
    if notes:
        notesToGoogleDoc(notes)
        deleteNotes(notes, keep)

# Used for testing. Not called by AWS Lambda
if __name__ == '__main__':
    main()
