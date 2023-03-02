##  Photini - a simple photo metadata editor.
##  http://github.com/jim-easterbrook/Photini
##  Copyright (C) 2022-23  Jim Easterbrook  jim@jim-easterbrook.me.uk
##
##  This program is free software: you can redistribute it and/or
##  modify it under the terms of the GNU General Public License as
##  published by the Free Software Foundation, either version 3 of the
##  License, or (at your option) any later version.
##
##  This program is distributed in the hope that it will be useful,
##  but WITHOUT ANY WARRANTY; without even the implied warranty of
##  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
##  General Public License for more details.
##
##  You should have received a copy of the GNU General Public License
##  along with this program.  If not, see
##  <http://www.gnu.org/licenses/>.

import codecs
from datetime import datetime, timedelta
from fractions import Fraction
import logging
import math
import pprint
import re

from photini.exiv2 import MetadataHandler
from photini.pyqt import QtCore, QtGui, qt_version_info, using_pyside

logger = logging.getLogger(__name__)

# photini.metadata imports these classes
__all__ = (
    'MD_Aperture', 'MD_CameraModel', 'MD_ContactInformation', 'MD_DateTime',
    'MD_Dimensions', 'MD_GPSinfo', 'MD_ImageRegion', 'MD_Int', 'MD_Keywords',
    'MD_LangAlt', 'MD_LensModel', 'MD_MultiLocation', 'MD_MultiString',
    'MD_Orientation', 'MD_Rating', 'MD_Rational', 'MD_Rights',
    'MD_SingleLocation', 'MD_Software', 'MD_String', 'MD_Thumbnail',
    'MD_Timezone', 'safe_fraction')


def safe_fraction(value, limit=True):
    # Avoid ZeroDivisionError when '0/0' used for zero values in Exif
    try:
        if isinstance(value, (list, tuple)):
            value = Fraction(*value)
        else:
            value = Fraction(value)
    except ZeroDivisionError:
        return Fraction(0.0)
    if limit:
        # round off excessively large denominators
        value = value.limit_denominator(1000000)
    return value


class MD_Value(object):
    # mixin for "metadata objects" - Python types with additional functionality
    _quiet = False

    @classmethod
    def from_ffmpeg(cls, file_value, tag):
        return cls(file_value)

    @classmethod
    def from_exiv2(cls, file_value, tag):
        return cls(file_value)

    def to_exiv2(self, tag):
        return {'Exif': self.to_exif,
                'Iptc': self.to_iptc,
                'Xmp': self.to_xmp}[tag.split('.')[0]]()

    def to_exif(self):
        return str(self)

    def to_iptc(self):
        return str(self)

    def to_xmp(self):
        return str(self)

    def merge(self, info, tag, other):
        result, merged, ignored = self.merge_item(self, other)
        if ignored:
            self.log_ignored(info, tag, other)
        elif merged:
            self.log_merged(info, tag, other)
            return self.__class__(result)
        return self

    def merge_item(self, this, other):
        if self.contains(this, other):
            return this, False, False
        if self.contains(other, this):
            return other, True, False
        return self.concat(this, other)

    def contains(self, this, other):
        return other == this

    def concat(self, this, other):
        return this, False, True

    @staticmethod
    def log_merged(info, tag, value):
        logger.info('%s: merged %s', info, tag)

    def log_replaced(self, info, tag, value):
        logger.log(
            (logging.WARNING, logging.INFO)[self._quiet],
            '%s: "%s" replaced by %s "%s"', info, str(self), tag, str(value))

    @classmethod
    def log_ignored(cls, info, tag, value):
        logger.log(
            (logging.WARNING, logging.INFO)[cls._quiet],
            '%s: ignored %s "%s"', info, tag, str(value))


class MD_UnmergableString(MD_Value, str):
    def __new__(cls, value):
        if value is None:
            value = ''
        elif isinstance(value, str):
            value = value.strip()
        return super(MD_UnmergableString, cls).__new__(cls, value)

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if isinstance(file_value, list):
            file_value = ' // '.join(file_value)
        return cls(file_value)

    def contains(self, this, other):
        return other in this


class MD_String(MD_UnmergableString):
    def concat(self, this, other):
        return this + ' // ' + other, True, False


class MD_Software(MD_String):
    @classmethod
    def from_exiv2(cls, file_value, tag):
        if tag.startswith('Iptc'):
            file_value = ' v'.join(x for x in file_value if x)
        return cls(file_value)

    def to_iptc(self):
        return self.split(' v')


class MD_Dict(MD_Value, dict):
    def __init__(self, value):
        # can initialise from a string containing comma separated values
        if isinstance(value, str):
            value = value.split(',')
        # or a list of values
        if isinstance(value, (tuple, list)):
            value = zip(self._keys, value)
        # initialise all keys to None
        result = dict.fromkeys(self._keys)
        # update with any supplied values
        if value:
            result.update(value)
        # let sub-classes do any data manipulation
        result = self.convert(result)
        super(MD_Dict, self).__init__(result)

    @staticmethod
    def convert(value):
        for key in value:
            if isinstance(value[key], str):
                value[key] = value[key].strip() or None
        return value

    def __setattr__(self, name, value):
        raise TypeError(
            "{} does not support item assignment".format(self.__class__))

    def __setitem__(self, key, value):
        raise TypeError(
            "{} does not support item assignment".format(self.__class__))

    def __bool__(self):
        return any([x is not None for x in self.values()])

    def to_exif(self):
        return [self[x] for x in self._keys]

    def __str__(self):
        return '\n'.join('{}: {}'.format(k, v) for (k, v) in self.items() if v)


class MD_DateTime(MD_Dict):
    # store date and time with "precision" to store how much is valid
    # tz_offset is stored in minutes
    _keys = ('datetime', 'precision', 'tz_offset')

    @classmethod
    def convert(cls, value):
        value['precision'] = value['precision'] or 7
        if value['datetime']:
            value['datetime'] = cls.truncate_datetime(
                value['datetime'], value['precision'])
        if value['precision'] <= 3:
            value['tz_offset'] = None
        return value

    _replace = (('microsecond', 0), ('second', 0),
                ('minute',      0), ('hour',   0),
                ('day',         1), ('month',  1))

    @classmethod
    def truncate_datetime(cls, date_time, precision):
        return date_time.replace(**dict(cls._replace[:7 - precision]))

    _tz_re = re.compile(r'(.*?)([+-])(\d{1,2}):?(\d{1,2})$')
    _subsec_re = re.compile(r'(.*?)\.(\d+)$')
    _time_re = re.compile(r'(.*?)[T ](\d{1,2}):?(\d{1,2})?:?(\d{1,2})?$')
    _date_re = re.compile(r'(\d{1,4})[:-]?(\d{1,2})?[:-]?(\d{1,2})?$')

    @classmethod
    def from_ISO_8601(cls, datetime_string, sub_sec_string=None):
        """Sufficiently general ISO 8601 parser.

        See https://en.wikipedia.org/wiki/ISO_8601

        """
        if not datetime_string:
            return cls([])
        unparsed = datetime_string
        precision = 7
        # extract time zone
        match = cls._tz_re.match(unparsed)
        if match:
            unparsed, sign, hours, minutes = match.groups()
            tz_offset = (int(hours) * 60) + int(minutes)
            if sign == '-':
                tz_offset = -tz_offset
        elif unparsed[-1] == 'Z':
            tz_offset = 0
            unparsed = unparsed[:-1]
        else:
            tz_offset = None
        # extract sub seconds
        if not sub_sec_string:
            match = cls._subsec_re.match(unparsed)
            if match:
                unparsed, sub_sec_string = match.groups()
        if sub_sec_string:
            microsecond = int((sub_sec_string + '000000')[:6])
        else:
            microsecond = 0
            precision = 6
        # extract time
        match = cls._time_re.match(unparsed)
        if match:
            groups = match.groups('0')
            unparsed = groups[0]
            hour, minute, second = [int(x) for x in groups[1:]]
            if match.lastindex < 4:
                precision = 2 + match.lastindex
        else:
            hour, minute, second = 0, 0, 0
            precision = 3
        # extract date
        match = cls._date_re.match(unparsed)
        if match:
            year, month, day = [int(x) for x in match.groups('1')]
            if match.lastindex < 3:
                precision = match.lastindex
            if day == 0:
                day = 1
                precision = 2
            if month == 0:
                month = 1
                precision = 1
        else:
            raise ValueError(
                'Cannot parse datetime "{}"'.format(datetime_string))
        return cls((
            datetime(year, month, day, hour, minute, second, microsecond),
            precision, tz_offset))

    _fmt_elements = ('%Y', '-%m', '-%d', 'T%H', ':%M', ':%S', '.%f')

    def to_ISO_8601(self, precision=None, time_zone=True):
        if precision is None:
            precision = self['precision']
        fmt = ''.join(self._fmt_elements[:precision])
        datetime_string = self['datetime'].strftime(fmt)
        if precision > 6:
            # truncate subsecond to 3 digits
            datetime_string = datetime_string[:-3]
        if precision > 3 and time_zone and self['tz_offset'] is not None:
            # add time zone
            minutes = self['tz_offset']
            if minutes >= 0:
                datetime_string += '+'
            else:
                datetime_string += '-'
                minutes = -minutes
            datetime_string += '{:02d}:{:02d}'.format(
                minutes // 60, minutes % 60)
        return datetime_string

    @classmethod
    def from_ffmpeg(cls, file_value, tag):
        return cls.from_ISO_8601(file_value)

    # many quicktime movies use Apple's 1904 timestamp zero point
    _qt_offset = (datetime(1970, 1, 1) - datetime(1904, 1, 1)).total_seconds()

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if tag.startswith('Exif'):
            return cls.from_exif(file_value)
        if tag.startswith('Iptc'):
            return cls.from_iptc(file_value)
        if tag.startswith('Xmp.video'):
            try:
                time_stamp = int(file_value)
            except Exception:
                # not an integer timestamp
                return cls([])
            if not time_stamp:
                return cls([])
            # assume date should be in range 1970 to 2034
            if time_stamp > cls._qt_offset:
                time_stamp -= cls._qt_offset
            return cls((datetime.utcfromtimestamp(time_stamp), 6, None))
        return cls.from_ISO_8601(file_value)

    # From the Exif spec: "The format is "YYYY:MM:DD HH:MM:SS" with time
    # shown in 24-hour format, and the date and time separated by one
    # blank character [20.H]. When the date and time are unknown, all
    # the character spaces except colons (":") may be filled with blank
    # characters.

    # Although the standard says "all", I've seen examples where some of
    # the values are spaces, e.g. "2004:01:     :  :  ". I assume this
    # represents a reduced precision. In Photini we write a full
    # resolution datetime and get the precision from the Xmp value.
    @classmethod
    def from_exif(cls, file_value):
        datetime_string, sub_sec_string = file_value
        if not datetime_string:
            return cls([])
        # check for blank values
        while datetime_string[-2:] == '  ':
            datetime_string = datetime_string[:-3]
        # do conversion
        return cls.from_ISO_8601(datetime_string, sub_sec_string=sub_sec_string)

    def to_exif(self):
        datetime_string = self.to_ISO_8601(
            precision=max(self['precision'], 6), time_zone=False)
        date_string = datetime_string[:10].replace('-', ':')
        time_string = datetime_string[11:19]
        sub_sec_string = datetime_string[20:]
        return date_string + ' ' + time_string, sub_sec_string

    # The exiv2 library parses correctly formatted IPTC date & time and
    # gives us integer values for each element. If the date or time is
    # malformed we get a string instead, and ignore it.

    # The date (and time?) can have missing values represented by 00
    # according to
    # https://www.iptc.org/std/photometadata/specification/IPTC-PhotoMetadata#date-created
    @classmethod
    def from_iptc(cls, file_value):
        date_value, time_value = file_value
        if not date_value:
            return cls([])
        if isinstance(date_value, str):
            # Exiv2 couldn't read malformed date, let our parser have a go
            if isinstance(time_value, str):
                date_value += 'T' + time_value
            return cls.from_ISO_8601(date_value)
        if date_value['year'] == 0:
            return cls([])
        precision = 3
        if isinstance(time_value, dict):
            tz_offset = (time_value['tzHour'] * 60) + time_value['tzMinute']
            del time_value['tzHour'], time_value['tzMinute']
            # all-zero time is assumed to be no time info
            if any(time_value.values()):
                precision = 6
        else:
            # missing or malformed time
            time_value = {}
            tz_offset = None
        if date_value['day'] == 0:
            date_value['day'] = 1
            precision = 2
        if date_value['month'] == 0:
            date_value['month'] = 1
            precision = 1
        return cls((datetime(**date_value, **time_value), precision, tz_offset))

    def to_iptc(self):
        precision = self['precision']
        datetime = self['datetime']
        year, month, day = datetime.year, datetime.month, datetime.day
        if precision < 2:
            month = 0
        if precision < 3:
            day = 0
        date_value = year, month, day
        if precision < 4:
            time_value = None
        else:
            tz_offset = self['tz_offset']
            if tz_offset is None:
                tz_hr, tz_min = 0, 0
            else:
                tz_hr, tz_min = tz_offset // 60, tz_offset % 60
            time_value = (
                datetime.hour, datetime.minute, datetime.second, tz_hr, tz_min)
        return date_value, time_value

    # XMP uses extended ISO 8601, but the time cannot be hours only. See
    # p75 of
    # https://partners.adobe.com/public/developer/en/xmp/sdk/XMPspecification.pdf
    # According to p71, when converting Exif values with no time zone,
    # local time zone should be assumed. However, the MWG guidelines say
    # this must not be assumed to be the time zone where the photo is
    # processed. It also says the XMP standard has been revised to make
    # time zone information optional.
    def to_xmp(self):
        precision = self['precision']
        if precision == 4:
            precision = 5
        return self.to_ISO_8601(precision=precision)

    def __bool__(self):
        return bool(self['datetime'])

    def __str__(self):
        return self.to_ISO_8601()

    def to_utc(self):
        if self['tz_offset']:
            return self['datetime'] - timedelta(minutes=self['tz_offset'])
        return self['datetime']

    def merge(self, info, tag, other):
        if other == self or not other:
            return self
        if other['datetime'] != self['datetime']:
            verbose = (other['datetime'] != self.truncate_datetime(
                self['datetime'], other['precision']))
            # datetime values differ, choose self or other
            if (self['tz_offset'] in (None, 0)) != (other['tz_offset'] in (None, 0)):
                if self['tz_offset'] in (None, 0):
                    # other has "better" time zone info so choose it
                    if verbose:
                        self.log_replaced(info, tag, other)
                    return other
                # self has better time zone info
                if verbose:
                    self.log_ignored(info, tag, other)
                return self
            if other['precision'] > self['precision']:
                # other has higher precision so choose it
                if verbose:
                    self.log_replaced(info, tag, other)
                return other
            if verbose:
                self.log_ignored(info, tag, other)
            return self
        # datetime values agree, merge other info
        result = dict(self)
        if tag.startswith('Xmp'):
            # other is Xmp, so has trusted timezone and precision
            result['precision'] = other['precision']
            result['tz_offset'] = other['tz_offset']
        else:
            # use higher precision
            if other['precision'] > self['precision']:
                result['precision'] = other['precision']
            # only trust non-zero timezone (IPTC defaults to zero)
            if (self['tz_offset'] in (None, 0)
                    and other['tz_offset'] not in (None, 0)):
                result['tz_offset'] = other['tz_offset']
        return MD_DateTime(result)


class MD_LensSpec(MD_Dict):
    # simple class to store lens "specificaton"
    _keys = ('min_fl', 'max_fl', 'min_fl_fn', 'max_fl_fn')
    _quiet = True

    @staticmethod
    def convert(value):
        for key in value:
            value[key] = safe_fraction(value[key] or 0)
        return value

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not file_value:
            return cls([])
        if isinstance(file_value, str):
            file_value = file_value.split()
        if 'CanonCs' in tag:
            long_focal, short_focal, focal_units = [int(x) for x in file_value]
            if focal_units == 0:
                return cls([])
            file_value = [(short_focal, focal_units), (long_focal, focal_units)]
        return cls(file_value)

    def to_xmp(self):
        return ' '.join(['{}/{}'.format(x.numerator, x.denominator)
                         for x in self.to_exif()])

    def __str__(self):
        return ','.join(['{:g}'.format(float(self[x])) for x in self._keys])


class MD_Thumbnail(MD_Dict):
    _keys = ('w', 'h', 'fmt', 'data', 'image')
    _quiet = True

    @staticmethod
    def image_from_data(data):
        # PyQt5 seems to be the only thing that can use memoryviews
        if isinstance(data, memoryview) and (
                using_pyside or qt_version_info >= (6, 0)):
            data = bytes(data)
        buf = QtCore.QBuffer()
        buf.setData(data)
        reader = QtGui.QImageReader(buf)
        fmt = reader.format().data().decode().upper()
        reader.setAutoTransform(False)
        image = reader.read()
        if image.isNull():
            raise RuntimeError(reader.errorString())
        return fmt, image

    @staticmethod
    def data_from_image(image, max_size=60000):
        buf = QtCore.QBuffer()
        buf.open(buf.OpenModeFlag.WriteOnly)
        quality = 95
        while quality > 10:
            image.save(buf, 'JPEG', quality)
            data = buf.data().data()
            if len(data) < max_size:
                return data
            quality -= 5
        return None

    @classmethod
    def convert(cls, value):
        value['fmt'] = value['fmt'] or 'JPEG'
        if value['data'] and not value['image']:
            value['fmt'], value['image'] = cls.image_from_data(value['data'])
        if not value['image']:
            return {}
        value['w'] = value['image'].width()
        value['h'] = value['image'].height()
        if value['data'] and len(value['data']) >= 60000:
            # don't keep unusably large amount of data
            value['data'] = None
        return value

    def to_exif(self):
        fmt, data = self['fmt'], self['data']
        if not data:
            fmt = 'JPEG'
            data = self.data_from_image(self['image'])
        if not data:
            return None, None, None, None
        fmt = (None, 6)[fmt == 'JPEG']
        return self['w'], self['h'], fmt, data

    def to_xmp(self):
        fmt, data = self['fmt'], self['data']
        if fmt != 'JPEG':
            data = None
        if not data:
            fmt = 'JPEG'
            data = self.data_from_image(self['image'], max_size=2**32)
        data = codecs.encode(data, 'base64_codec').decode('ascii')
        return [{
            'xmpGImg:width': str(self['w']),
            'xmpGImg:height': str(self['h']),
            'xmpGImg:format': fmt,
            'xmpGImg:image': data,
            }]

    def __str__(self):
        result = '{fmt} thumbnail, {w}x{h}'.format(**self)
        if self['data']:
            result += ', {} bytes'.format(len(self['data']))
        return result


class MD_Collection(MD_Dict):
    # class for a group of independent items, each of which is an MD_Value
    _type = {}
    _default_type = MD_String

    @classmethod
    def get_type(cls, key):
        if key in cls._type:
            return cls._type[key]
        return cls._default_type

    @classmethod
    def convert(cls, value):
        for key in value:
            if not value[key]:
                continue
            value[key] = cls.get_type(key)(value[key])
        return value

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not (file_value and any(file_value)):
            return cls([])
        value = dict(zip(cls._keys, file_value))
        for key in value:
            value[key] = cls.get_type(key).from_exiv2(value[key], tag)
        return cls(value)

    def to_exif(self):
        return [(self[x] or None) and self[x].to_exif() for x in self._keys]

    def to_iptc(self):
        return [(self[x] or None) and self[x].to_iptc() for x in self._keys]

    def to_xmp(self):
        return [(self[x] or None) and self[x].to_xmp() for x in self._keys]

    def merge(self, info, tag, other):
        if other == self:
            return self
        result = dict(self)
        for key in other:
            if other[key] is None:
                continue
            if key in result and result[key] is not None:
                result[key], merged, ignored = result[key].merge_item(
                                                        result[key], other[key])
            else:
                result[key] = other[key]
                merged, ignored = True, False
            if ignored:
                self.log_ignored(info, tag, {key: str(other[key])})
            elif merged:
                self.log_merged(info, tag, {key: str(other[key])})
        return self.__class__(result)


class MD_ContactInformation(MD_Collection):
    _keys = ('plus:LicensorExtendedAddress', 'plus:LicensorStreetAddress',
             'plus:LicensorCity', 'plus:LicensorPostalCode',
             'plus:LicensorRegion', 'plus:LicensorCountry',
             'plus:LicensorTelephone1',
             'plus:LicensorEmail', 'plus:LicensorURL')

    _ci_map = {
        'Iptc4xmpCore:CiAdrExtadr': 'plus:LicensorStreetAddress',
        'Iptc4xmpCore:CiAdrCity':   'plus:LicensorCity',
        'Iptc4xmpCore:CiAdrCtry':   'plus:LicensorCountry',
        'Iptc4xmpCore:CiEmailWork': 'plus:LicensorEmail',
        'Iptc4xmpCore:CiTelWork':   'plus:LicensorTelephone1',
        'Iptc4xmpCore:CiAdrPcode':  'plus:LicensorPostalCode',
        'Iptc4xmpCore:CiAdrRegion': 'plus:LicensorRegion',
        'Iptc4xmpCore:CiUrlWork':   'plus:LicensorURL',
        }

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if tag == 'Xmp.iptc.CreatorContactInfo':
            file_value = file_value or {}
            file_value = {(cls._ci_map[k], v) for (k, v) in file_value.items()}
            if 'plus:LicensorStreetAddress' in file_value:
                line1, sep, line2 = file_value[
                    'plus:LicensorStreetAddress'].partition('\n')
                if line2:
                    file_value['plus:LicensorExtendedAddress'] = line1
                    file_value['plus:LicensorStreetAddress'] = line2
        elif file_value:
            file_value = file_value[0]
        return cls(file_value)

    def to_xmp(self):
        return [self]


class MD_Tuple(MD_Value, tuple):
    # class for structured XMP data such as locations or image regions
    def __new__(cls, value=[]):
        value = value or []
        temp = []
        for item in value:
            if not isinstance(item, cls._type):
                item = cls._type(item)
            temp.append(item)
        while temp and not temp[-1]:
            temp = temp[:-1]
        return super(MD_Tuple, cls).__new__(cls, temp)

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not file_value:
            return cls()
        # Exif and IPTC only store one item, XMP stores any number
        if tag.startswith('Xmp'):
            file_value = [cls._type.from_exiv2(x, tag) for x in file_value]
        else:
            file_value = [cls._type.from_exiv2(file_value, tag)]
        return cls(file_value)

    def to_exif(self):
        return self and self[0].to_exif()

    def to_iptc(self):
        return self and self[0].to_iptc()

    def to_xmp(self):
        return [x.to_xmp() for x in self]

    def merge(self, info, tag, other):
        result = self
        for item in other:
            if not isinstance(item, self._type):
                item = self._type(item)
            idx = result.index(item)
            result = list(result)
            if idx < len(result):
                result[idx] = result[idx].merge(info, tag, item)
            else:
                self.log_merged(info, tag, item)
                result.append(item)
            result = self.__class__(result)
        return result

    def __str__(self):
        return '\n\n'.join(str(x) for x in self)


class LangAltDict(dict):
    # Modified dict that keeps track of a default language.
    DEFAULT = 'x-default'

    def __init__(self, value={}):
        super(LangAltDict, self).__init__()
        self.compact = False    # controls format of str() representation
        self._default_lang = ''
        if isinstance(value, str):
            value = value.strip()
        if not value:
            value = {QtCore.QLocale.system().bcp47Name() or self.DEFAULT: ''}
        elif isinstance(value, str):
            value = {self.DEFAULT: value}
        for k, v in value.items():
            self[k] = v
        if isinstance(value, LangAltDict):
            self._default_lang = value._default_lang
        # set default_lang
        if self._default_lang:
            return
        # Exiv2 doesn't preserve the order of items, so we can't assume
        # the first item is the default language. Use the locale
        # instead.
        lang = QtCore.QLocale.system().bcp47Name()
        if not lang:
            return
        for lang in (lang, lang.split('-')[0]):
            self._default_lang = self.find_key(lang)
            if self._default_lang:
                return

    def find_key(self, key):
        # languages are case insensitive
        key = key or ''
        key = key.lower()
        for k in self:
            if k.lower() == key:
                return k
        return ''

    def __contains__(self, key):
        return bool(self.find_key(key))

    def __getitem__(self, key):
        old_key = self.find_key(key)
        if old_key:
            key = old_key
        else:
            self[key] = ''
        return super(LangAltDict, self).__getitem__(key)

    def __setitem__(self, key, value):
        old_key = self.find_key(key)
        if old_key and old_key != key:
            # new key does not have same case as old one
            if self._default_lang == old_key:
                self._default_lang = key
            del self[old_key]
        super(LangAltDict, self).__setitem__(key, value)
        # Check for empty or duplicate 'x-default' value
        dflt_key = self.find_key(self.DEFAULT)
        if dflt_key == key or not dflt_key:
            return
        dflt_value = super(LangAltDict, self).__getitem__(dflt_key)
        if (not dflt_value) or (dflt_value == value):
            if self._default_lang == dflt_key:
                self._default_lang = key
            del self[dflt_key]

    def __bool__(self):
        return any(self.values())

    def __eq__(self, other):
        if isinstance(other, LangAltDict):
            return not self.__ne__(other)
        return super(LangAltDict, self).__eq__(other)

    def __ne__(self, other):
        if isinstance(other, LangAltDict):
            if self._default_lang != other._default_lang:
                return True
        return super(LangAltDict, self).__ne__(other)

    def __str__(self):
        result = []
        for key in self:
            if key != self.DEFAULT:
                if self.compact:
                    result.append('[{}]'.format(key))
                else:
                    result.append('-- {} --'.format(key))
            result.append(self[key])
        if self.compact:
            return ' '.join(result)
        return '\n'.join(result)

    def _sort_key(self, key):
        key = key or ''
        key = key.lower()
        if key == self.DEFAULT:
            return ' '
        if key == self._default_lang.lower():
            return '!'
        return key

    def keys(self):
        result = list(super(LangAltDict, self).keys())
        result.sort(key=self._sort_key)
        return result

    def __iter__(self):
        return iter(self.keys())

    def best_match(self, lang=None):
        if len(self) == 1:
            return self.default_text()
        lang = lang or QtCore.QLocale.system().bcp47Name()
        if not lang:
            return self.default_text()
        langs = [lang]
        if '-' in lang:
            langs.append(lang.split('-')[0])
        for lang in langs:
            k = self.find_key(lang)
            if k:
                return self[k]
            lang = lang.lower()
            for k in self:
                if k.lower().startswith(lang):
                    return self[k]
        return self.default_text()

    def strip(self):
        return dict((k, v.strip()) for (k, v) in self.items())

    def default_text(self):
        return self[self.keys()[0]]

    def get_default_lang(self):
        return self._default_lang or self.DEFAULT

    def set_default_lang(self, lang):
        self._default_lang = lang
        new_value = self[lang]
        key = self.find_key(self.DEFAULT)
        if not key:
            return
        old_value = super(LangAltDict, self).__getitem__(key)
        del self[key]
        if new_value in old_value:
            new_value = old_value
        elif old_value not in new_value:
            new_value += ' // ' + old_value
        self[lang] = new_value


class MD_LangAlt(LangAltDict, MD_Value):
    # MD_LangAlt values are a sequence of RFC3066 language tag keys and
    # text values. The sequence can have a single default value, but if
    # it has more than one value, the default should be repeated with a
    # language tag. See
    # https://developer.adobe.com/xmp/docs/XMPNamespaces/XMPDataTypes/#language-alternative

    def to_exif(self):
        # Xmp spec says to store only the default language in Exif
        if not self:
            return None
        return self.default_text()

    def to_iptc(self):
        return self.to_exif()

    def to_xmp(self):
        if not self:
            return None
        if len(self) == 1:
            return dict(self)
        default_lang = self.get_default_lang()
        result = {self.DEFAULT: self[default_lang],
                  default_lang: self[default_lang]}
        # don't save empty values
        for k, v in self.items():
            if v:
                result[k] = v
        return result

    def merge(self, info, tag, other):
        other = LangAltDict(other)
        if other == self:
            return self
        result = LangAltDict(self)
        for key, value in other.items():
            if key == self.DEFAULT:
                # try to find matching value
                for k, v in result.items():
                    if value in v or v in value:
                        key = k
                        break
            else:
                # try to find matching language
                key = result.find_key(key) or key
            if key not in result:
                result[key] = value
            elif value in result[key]:
                continue
            elif result[key] in value:
                result[key] = value
            else:
                result[key] += ' // ' + value
            self.log_merged(info + '[' + key + ']', tag, value)
        return self.__class__(result)


class MD_Rights(MD_Collection):
    # stores IPTC rights information
    _keys = ('UsageTerms', 'WebStatement')
    _default_type = MD_UnmergableString
    _type = {'UsageTerms': MD_LangAlt}


class MD_CameraModel(MD_Collection):
    _keys = ('make', 'model', 'serial_no')
    _default_type = MD_UnmergableString
    _quiet = True

    def convert(self, value):
        if value['model'] == 'unknown':
            value['model'] = None
        return super(MD_CameraModel, self).convert(value)

    def __str__(self):
        return str(dict([(x, y) for x, y in self.items() if y]))

    def get_name(self, inc_serial=True):
        result = []
        # start with 'model'
        if self['model']:
            result.append(self['model'])
        # only add 'make' if it's not part of model
        if self['make']:
            if not (result
                    and self['make'].split()[0].lower() in result[0].lower()):
                result = [self['make']] + result
        # add serial no if a unique answer is needed
        if inc_serial and self['serial_no']:
            result.append('(S/N: ' + self['serial_no'] + ')')
        return ' '.join(result)


class MD_LensModel(MD_Collection):
    _keys = ('make', 'model', 'serial_no', 'spec')
    _default_type = MD_UnmergableString
    _type = {'spec': MD_LensSpec}
    _quiet = True

    def convert(self, value):
        if value['model'] in ('n/a', '(0)', '65535'):
            value['model'] = None
        if value['serial_no'] == '0000000000':
            value['serial_no'] = None
        return super(MD_LensModel, self).convert(value)

    def get_name(self, inc_serial=True):
        result = []
        # start with 'model'
        if self['model']:
            result.append(self['model'])
        # only add 'make' if it's not part of model
        if self['make']:
            if not (result
                    and self['make'].split()[0].lower() in result[0].lower()):
                result = [self['make']] + result
        if inc_serial and self['serial_no']:
            result.append('(S/N: ' + self['serial_no'] + ')')
        if self['spec'] and not result:
            # generic name based on spec
            fl = [float(self['spec']['min_fl']), float(self['spec']['max_fl'])]
            fl = '–'.join(['{:g}'.format(x) for x in fl if x])
            fn = [float(self['spec']['min_fl_fn']),
                  float(self['spec']['max_fl_fn'])]
            fn = '–'.join(['{:g}'.format(x) for x in fn if x])
            if fl:
                model = fl + ' mm'
                if fn:
                    model += ' ƒ/' + fn
                result.append(model)
        return ' '.join(result)


class MD_MultiString(MD_Value, tuple):
    def __new__(cls, value):
        value = value or []
        if isinstance(value, str):
            value = value.split(';')
        value = filter(bool, [x.strip() for x in value])
        return super(MD_MultiString, cls).__new__(cls, value)

    def to_exif(self):
        return ';'.join(self)

    def to_iptc(self):
        return tuple(self)

    def to_xmp(self):
        return tuple(self)

    def __str__(self):
        return '; '.join(self)

    def merge(self, info, tag, other):
        merged = False
        result = list(self)
        for item in other:
            if tag.split('.')[0] == 'Iptc':
                # IPTC-IIM data can be truncated version of existing value
                if item not in [MetadataHandler.truncate_iptc(tag, x)
                                for x in result]:
                    result.append(item)
                    merged = True
            elif item not in result:
                result.append(item)
                merged = True
        if merged:
            self.log_merged(info, tag, other)
            return MD_MultiString(result)
        return self


class MD_Keywords(MD_MultiString):
    _machine_tag = re.compile(r'^(.+):(.+)=(.+)$')

    def human_tags(self):
        return [x for x in self if not self._machine_tag.match(x)]

    def machine_tags(self):
        # yield keyword, (ns, predicate, value) for each machine tag
        for keyword in self:
            match = self._machine_tag.match(keyword)
            if match:
                yield keyword, match.groups()


class MD_Int(MD_Value, int):
    def __new__(cls, value):
        if value is None:
            return None
        return super(MD_Int, cls).__new__(cls, value)

    def to_exif(self):
        return self

    def __bool__(self):
        # reinterpret to mean "has a value", even if the value is zero
        return True


class MD_Orientation(MD_Int):
    @classmethod
    def from_ffmpeg(cls, file_value, tag):
        mapping = {'0': 1, '90': 6, '180': 3, '-90': 8}
        if file_value not in mapping:
            raise ValueError('unrecognised orientation {}'.format(file_value))
        return cls(mapping[file_value])


class MD_Timezone(MD_Int):
    _quiet = True

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if file_value is None:
            return None
        if tag == 'Exif.Image.TimeZoneOffset':
            # convert hours to minutes
            file_value = file_value * 60
        return cls(file_value)


class MD_Float(MD_Value, float):
    def __new__(cls, value):
        if value is None:
            return None
        return super(MD_Float, cls).__new__(cls, value)

    def __bool__(self):
        # reinterpret to mean "has a value", even if the value is zero
        return True


class MD_Rating(MD_Float):
    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not file_value:
            return None
        if tag in ('Exif.Image.RatingPercent', 'Xmp.MicrosoftPhoto.Rating'):
            value = 1.0 + (float(file_value) / 25.0)
        else:
            value = min(max(float(file_value), -1.0), 5.0)
        return cls(value)

    def to_exif(self):
        return str(int(self + 1.5) - 1)


class MD_Rational(MD_Value, Fraction):
    def __new__(cls, value):
        if value is None:
            return None
        return super(MD_Rational, cls).__new__(cls, safe_fraction(value))

    def to_exif(self):
        return self

    def to_xmp(self):
        return '{}/{}'.format(self.numerator, self.denominator)

    def __bool__(self):
        # reinterpret to mean "has a value", even if the value is zero
        return True

    def __str__(self):
        return str(float(self))


class MD_Altitude(MD_Rational):
    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not all(file_value):
            return None
        altitude, ref = file_value
        altitude = safe_fraction(altitude)
        if ref in (b'\x01', '1'):
            altitude = -altitude
        return cls(altitude)

    def to_exif(self):
        altitude = self
        if altitude < 0:
            altitude = -altitude
            ref = b'\x01'
        else:
            ref = b'\x00'
        return altitude, ref

    def to_xmp(self):
        altitude = self
        if altitude < 0:
            altitude = -altitude
            ref = '1'
        else:
            ref = '0'
        return '{}/{}'.format(altitude.numerator, altitude.denominator), ref


class MD_Coordinate(MD_Rational):
    @classmethod
    def from_exiv2(cls, file_value, tag):
        if tag.startswith('Exif'):
            return cls.from_exif(file_value)
        return cls.from_xmp(file_value)

    @classmethod
    def from_exif(cls, value):
        if not all(value):
            return None
        value, ref = value
        value = [safe_fraction(x, limit=False) for x in value]
        degrees, minutes, seconds = value
        degrees += (minutes / 60) + (seconds / 3600)
        if ref in ('S', 'W'):
            degrees = -degrees
        return cls(degrees)

    @classmethod
    def from_xmp(cls, value):
        if not value:
            return None
        ref = value[-1]
        if ref in ('N', 'E', 'S', 'W'):
            negative = ref in ('S', 'W')
            value = value[:-1]
        else:
            logger.warning('no direction in XMP GPSCoordinate: %s', value)
            negative = False
        if value[0] in ('+', '-'):
            logger.warning(
                'incorrect use of signed XMP GPSCoordinate: %s', value)
            if value[0] == '-':
                negative = not negative
            value = value[1:]
        value = [safe_fraction(x, limit=False) for x in value.split(',')]
        degrees, minutes = value[:2]
        degrees += (minutes / 60)
        if len(value) > 2:
            seconds = value[2]
            degrees += (seconds / 3600)
        if negative:
            degrees = -degrees
        return cls(degrees)

    def to_exif(self):
        degrees = self
        pstv = degrees >= 0
        if not pstv:
            degrees = -degrees
        # make degrees and minutes integer (not mandated by Exif, but typical)
        i = int(degrees)
        minutes = (degrees - i) * 60
        degrees = Fraction(i)
        i = int(minutes)
        seconds = (minutes - i) * 60
        minutes = Fraction(i)
        seconds = seconds.limit_denominator(1000000)
        return (degrees, minutes, seconds), pstv

    def to_xmp(self):
        pstv, degrees, minutes, seconds = self.to_exif()
        numbers = degrees, minutes, seconds
        if all([x.denominator == 1 for x in numbers]):
            return ('{:d},{:d},{:d}'.format(*[x.numerator for x in numbers]),
                    pstv)
        degrees = int(degrees)
        minutes = float(minutes + (seconds / 60))
        return '{:d},{:.8f}'.format(degrees, minutes), pstv

    def __str__(self):
        return '{:.6f}'.format(float(self))


class MD_Latitude(MD_Coordinate):
    def to_exif(self):
        numbers, pstv = super(MD_Latitude, self).to_exif()
        return numbers, ('S', 'N')[pstv]

    def to_xmp(self):
        string, pstv = super(MD_Latitude, self).to_xmp()
        return string + ('S', 'N')[pstv]


class MD_Longitude(MD_Coordinate):
    def to_exif(self):
        numbers, pstv = super(MD_Longitude, self).to_exif()
        return numbers, ('W', 'E')[pstv]

    def to_xmp(self):
        string, pstv = super(MD_Longitude, self).to_xmp()
        return string + ('W', 'E')[pstv]


class MD_GPSinfo(MD_Dict):
    # stores GPS information
    _keys = ('version_id', 'method', 'alt', 'lat', 'lon')

    @staticmethod
    def convert(value):
        value['version_id'] = value['version_id'] or b'\x02\x00\x00\x00'
        if not isinstance(value['alt'], MD_Altitude):
            value['alt'] = MD_Altitude(value['alt'])
        if not isinstance(value['lat'], MD_Latitude):
            value['lat'] = MD_Latitude(value['lat'])
        if not isinstance(value['lon'], MD_Longitude):
            value['lon'] = MD_Longitude(value['lon'])
        return value

    @classmethod
    def from_ffmpeg(cls, file_value, tag):
        if file_value:
            match = re.match(
                r'([-+]\d+\.\d+)([-+]\d+\.\d+)([-+]\d+\.\d+)/$', file_value)
            if match:
                return cls(zip(('lat', 'lon', 'alt'), match.groups()))
        return None

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if tag.startswith('Xmp.video'):
            return cls.from_ffmpeg(file_value, tag)
        version_id = file_value[0]
        method = MD_UnmergableString.from_exiv2(file_value[1], tag)
        alt = MD_Altitude.from_exiv2(file_value[2:4], tag)
        if tag.startswith('Exif'):
            lat = MD_Latitude.from_exif(file_value[4:6])
            lon = MD_Longitude.from_exif(file_value[6:8])
        else:
            if version_id:
                version_id = bytes([int(x) for x in version_id.split('.')])
            lat = MD_Latitude.from_xmp(file_value[4])
            lon = MD_Longitude.from_xmp(file_value[5])
        return cls((version_id, method, alt, lat, lon))

    def to_exif(self):
        if self['alt']:
            altitude, alt_ref = self['alt'].to_exif()
        else:
            altitude, alt_ref = None, None
        if self['lat']:
            lat_value, lat_ref = self['lat'].to_exif()
            lon_value, lon_ref = self['lon'].to_exif()
        else:
            lat_value, lat_ref, lon_value, lon_ref = None, None, None, None
        if self['method']:
            method = 'charset=Ascii ' + self['method']
        else:
            method = None
        return (self['version_id'], method,
                altitude, alt_ref, lat_value, lat_ref, lon_value, lon_ref)

    def to_xmp(self):
        version_id = '.'.join([str(x) for x in self['version_id']])
        if self['alt']:
            altitude, alt_ref = self['alt'].to_xmp()
        else:
            altitude, alt_ref = None, None
        if self['lat']:
            lat_string = self['lat'].to_xmp()
            lon_string = self['lon'].to_xmp()
        else:
            lat_string, lon_string = None, None
        return (version_id, self['method'],
                altitude, alt_ref, lat_string, lon_string)

    def merge_item(self, this, other):
        merged = False
        ignored = False
        result = dict(this)
        if not isinstance(other, MD_GPSinfo):
            other = MD_GPSinfo(other)
        # compare coordinates
        if other['lat']:
            if not result['lat']:
                # swap entirely
                result = dict(other)
                other = this
                merged = True
            elif (abs(float(other['lat']) -
                      float(result['lat'])) > 0.000001
                  or abs(float(other['lon']) -
                         float(result['lon'])) > 0.000001):
                # lat, lon differs, keep the one with altitude
                if other['alt'] and not result['alt']:
                    result = dict(other)
                    other = this
                ignored = True
        # now consider altitude
        if other['alt'] and not ignored:
            if not result['alt']:
                result['alt'] = other['alt']
                merged = True
            elif abs(float(other['alt']) - float(result['alt'])) > 0.01:
                # alt differs, can only keep one of them
                ignored = True
        return result, merged, ignored

    def __bool__(self):
        return any(self[k] for k in ('lat', 'lon', 'alt'))

    def __eq__(self, other):
        if not isinstance(other, MD_GPSinfo):
            return super(MD_GPSinfo, self).__eq__(other)
        if bool(other['alt']) != bool(self['alt']):
            return False
        if bool(other['lat']) != bool(self['lat']):
            return False
        if self['alt'] and abs(float(other['alt']) -
                               float(self['alt'])) > 0.001:
            return False
        if self['lat'] and abs(float(other['lat']) -
                               float(self['lat'])) > 0.0000001:
            return False
        if self['lon'] and abs(float(other['lon']) -
                               float(self['lon'])) > 0.0000001:
            return False
        return True


class MD_Aperture(MD_Rational):
    # store FNumber and APEX aperture as fractions
    # only FNumber is presented to the user, either is computed if missing
    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not any(file_value):
            return None
        f_number, apex = file_value
        if apex:
            apex = safe_fraction(apex)
        if not f_number:
            f_number = 2.0 ** (apex / 2.0)
        self = cls(f_number)
        if apex:
            self.apex = apex
        return self

    def to_exif(self):
        file_value = [self]
        if float(self) != 0:
            apex = getattr(self, 'apex', safe_fraction(math.log(self, 2) * 2.0))
            file_value.append(apex)
        return file_value

    def to_xmp(self):
        return ['{}/{}'.format(x.numerator, x.denominator)
                for x in self.to_exif()]

    def contains(self, this, other):
        return float(min(other, this)) > (float(max(other, this)) * 0.95)


class MD_FrameRate(MD_Rational):
    def contains(self, this, other):
        # exiv2 rounds 30000/1001 to 29.97
        return float(min(other, this)) > (float(max(other, this)) * 0.9999)


class MD_Dimensions(MD_Collection):
    _keys = ('width', 'height', 'frames', 'frame_rate')
    _default_type = MD_Int
    _type = {'frame_rate': MD_FrameRate}

    def duration(self):
        if self['frames'] and self['frame_rate']:
            return float(self['frames'] / self['frame_rate'])
        return 0.0


class MD_Location(MD_Collection):
    # stores IPTC defined location heirarchy
    _keys = ('Iptc4xmpExt:Sublocation', 'Iptc4xmpExt:City',
             'Iptc4xmpExt:ProvinceState', 'Iptc4xmpExt:CountryName',
             'Iptc4xmpExt:CountryCode', 'Iptc4xmpExt:WorldRegion',
             'Iptc4xmpExt:LocationName', 'Iptc4xmpExt:LocationId',
             'exif:GPSLatitude', 'exif:GPSLongitude', 'exif:GPSAltitude')
    _type = {'Iptc4xmpExt:CountryCode': MD_UnmergableString,
             'Iptc4xmpExt:LocationName': MD_LangAlt,
             'Iptc4xmpExt:LocationId': MD_MultiString,
             'exif:GPSLatitude': MD_Latitude,
             'exif:GPSLongitude': MD_Longitude,
             'exif:GPSAltitude': MD_Rational}

    def convert(self, value):
        if value['Iptc4xmpExt:CountryCode']:
            value['Iptc4xmpExt:CountryCode'] = value[
                'Iptc4xmpExt:CountryCode'].upper()
        return super(MD_Location, self).convert(value)

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if isinstance(file_value, list):
            # "legacy" list of string values
            return super(MD_Location, cls).from_exiv2(file_value, tag)
        for key in file_value:
            file_value[key] = cls.get_type(key).from_exiv2(file_value[key], tag)
        return cls(file_value)

    def to_xmp(self):
        if not self:
            # need a place holder for empty values
            return {'Iptc4xmpExt:City': ' '}
        return dict((k, v.to_xmp()) for (k, v) in self.items() if v)

    @classmethod
    def from_address(cls, gps, address, key_map):
        result = {}
        for key in cls._keys:
            result[key] = []
        for key in key_map:
            for foreign_key in key_map[key]:
                if foreign_key not in address or not address[foreign_key]:
                    continue
                if key in result and address[foreign_key] not in result[key]:
                    result[key].append(address[foreign_key])
                del(address[foreign_key])
        # only use one country code
        result['Iptc4xmpExt:CountryCode'] = result[
            'Iptc4xmpExt:CountryCode'][:1]
        # put unknown foreign keys in Sublocation
        for foreign_key in address:
            if address[foreign_key] in ' '.join(
                    result['Iptc4xmpExt:Sublocation']):
                continue
            result['Iptc4xmpExt:Sublocation'] = [
                '{}: {}'.format(foreign_key, address[foreign_key])
                ] + result['Iptc4xmpExt:Sublocation']
        for key in result:
            result[key] = ', '.join(result[key]) or None
        result['exif:GPSLatitude'] = gps['lat']
        result['exif:GPSLongitude'] = gps['lng']
        return cls(result)

    def as_latlon(self):
        if not (self['exif:GPSLatitude'] and self['exif:GPSLongitude']):
            return None
        return '{}, {}'.format(
            self['exif:GPSLatitude'], self['exif:GPSLongitude'])

    def __str__(self):
        if self['Iptc4xmpExt:LocationName']:
            self['Iptc4xmpExt:LocationName'].compact = True
        result = [(k.split(':')[1], self[k]) for k in (
            'Iptc4xmpExt:LocationName', 'Iptc4xmpExt:Sublocation',
            'Iptc4xmpExt:City', 'Iptc4xmpExt:ProvinceState',
            'Iptc4xmpExt:CountryName', 'Iptc4xmpExt:CountryCode',
            'Iptc4xmpExt:WorldRegion', 'Iptc4xmpExt:LocationId') if self[k]]
        latlon = self.as_latlon()
        if latlon:
            result.append(('Lat, lon', latlon))
        if self['exif:GPSAltitude']:
            result.append(('Altitude', '{}'.format(self['exif:GPSAltitude'])))
        return '\n'.join('{}: {}'.format(*x) for x in result)


class MD_MultiLocation(MD_Tuple):
    _type = MD_Location

    def index(self, other):
        for n, value in enumerate(self):
            if value == other:
                return n
        return len(self)


class MD_SingleLocation(MD_MultiLocation):
    def index(self, other):
        return 0


class ImageRegionItem(MD_Value, dict):
    ctypes = (
        {'name': {'en-GB': 'animal'},
         'definition': {
             'en-GB': 'A living organism different from humans or flora.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/animal'},
        {'name': {'en-GB': 'artwork'},
         'definition': {'en-GB': 'Artistic work.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/artwork'},
        {'name': {'en-GB': 'dividing line'},
         'definition': {
             'en-GB': 'A line expressing a visual division of the image, such'
             ' as a horizon.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/dividingLine'},
        {'name': {'en-GB': 'plant'},
         'definition': {
             'en-GB': 'A living organism different from humans and animals.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/plant'},
        {'name': {'en-GB': 'geographic area'},
         'definition': {
             'en-GB': 'A named area on the surface of the planet earth.'
             ' Specific details of the area can be expressed by other'
             ' metadata.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/geoArea'},
        {'name': {'en-GB': 'graphic'},
         'definition': {'en-GB': 'A graphic representation of information.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/graphic'},
        {'name': {'en-GB': 'machine-readable code'},
         'definition': {'en-GB': 'Optical label such as barcode or QR code.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/machineCode'},
        {'name': {'en-GB': 'human'},
         'definition': {'en-GB': 'A human being.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/human'},
        {'name': {'en-GB': 'product'},
         'definition': {
             'en-GB': 'A thing that was produced and can be handed over.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/product'},
        {'name': {'en-GB': 'text'},
         'definition': {'en-GB': 'Human readable script of any language.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/text'},
        {'name': {'en-GB': 'building'},
         'definition': {
             'en-GB': 'A structure with walls and roof in most cases.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/building'},
        {'name': {'en-GB': 'vehicle'},
         'definition': {
             'en-GB': 'An object used for transporting something, like car,'
             ' train, ship, plane or bike.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/vehicle'},
        {'name': {'en-GB': 'food'},
         'definition': {
             'en-GB': 'Substances providing nutrition for a living body.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/food'},
        {'name': {'en-GB': 'clothing'},
         'definition': {'en-GB': 'Something worn to cover the body.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/clothing'},
        {'name': {'en-GB': 'rock formation'},
         'definition': {'en-GB': 'A special formation of stone mass.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/rockFormation'},
        {'name': {'en-GB': 'body of water'},
         'definition': {
             'en-GB': 'A significant accumulation of water. Including a'
             ' waterfall, a geyser and other phenomena of water.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregiontype/bodyOfWater'},
        )

    roles = (
        {'name': {'en-GB': 'cropping'},
         'definition': {'en-GB': 'Image region can be used for any cropping.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/cropping'},
        {'name': {'en-GB': 'recommended cropping'},
         'definition': {'en-GB': 'Image region is recommended for cropping.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/recomCropping'},
        {'name': {'en-GB': 'landscape format cropping'},
         'definition': {
             'en-GB': 'Image region suggested for cropping in landscape format.'
             ' Use for images of non-landscape format.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/landscapeCropping'},
        {'name': {'en-GB': 'portrait format cropping'},
         'definition': {
             'en-GB': 'Image region suggested for cropping in portrait format.'
             ' Use for images of non-portrait format.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/portraitCropping'},
        {'name': {'en-GB': 'square format cropping'},
         'definition': {
             'en-GB': 'Image region suggested for cropping in square format.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/squareCropping'},
        {'name': {'en-GB': 'composite image item'},
         'definition': {
             'en-GB': 'Image region of an item in a composite image.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/compositeImageItem'},
        {'name': {'en-GB': 'copyright region'},
         'definition': {
             'en-GB': 'Image region with a copyright different from the'
             ' copyright of the whole picture.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/copyrightRegion'},
        {'name': {'en-GB': 'subject area'},
         'definition': {
             'en-GB': 'Image region contains a subject in the overall scene.'
             ' Multiple regions of an image may be set as subject area.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/subjectArea'},
        {'name': {'en-GB': 'main subject area'},
         'definition': {
             'en-GB': 'Image region contains the main subject in the overall'
             ' scene. Same as the Exif SubjectArea. Only a single region of an'
             ' image may be set as main subject area.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/mainSubjectArea'},
        {'name': {'en-GB': 'area of interest'},
         'definition': {
             'en-GB': 'Image region contains a thing of special interest to'
             ' the viewer.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/areaOfInterest'},
        {'name': {'en-GB': 'business use'},
         'definition': {
             'en-GB': 'Image region is dedicated to a specific business use.'
             ' In addition a more granular role could be expressed by a term'
             ' of a role CV of this business.'},
         'uri': 'http://cv.iptc.org/newscodes/imageregionrole/businessUse'},
        )

    @classmethod
    def from_exiv2(cls, file_value, tag):
        if not file_value:
            return None
        if tag.startswith('Xmp'):
            return file_value
        # Convert Exif.Photo.SubjectArea to an image region. See
        # https://www.iptc.org/std/photometadata/documentation/userguide/#_mapping_exif_subjectarea_iptc_image_region
        if len(file_value) == 2:
            region = {'Iptc4xmpExt:rbShape': 'polygon',
                      'Iptc4xmpExt:rbVertices': [{
                          'Iptc4xmpExt:rbX': file_value[0],
                          'Iptc4xmpExt:rbY': file_value[1]}]}
        elif len(file_value) == 3:
            region = {'Iptc4xmpExt:rbShape': 'circle',
                      'Iptc4xmpExt:rbX': file_value[0],
                      'Iptc4xmpExt:rbY': file_value[1],
                      'Iptc4xmpExt:rbRx': file_value[2] // 2}
        elif len(file_value) == 4:
            region = {'Iptc4xmpExt:rbShape': 'rectangle',
                      'Iptc4xmpExt:rbX': file_value[0] - (file_value[2] // 2),
                      'Iptc4xmpExt:rbY': file_value[1] - (file_value[3] // 2),
                      'Iptc4xmpExt:rbW': file_value[2],
                      'Iptc4xmpExt:rbH': file_value[3]}
        else:
            return None
        region['Iptc4xmpExt:rbUnit'] = 'pixel'
        return {
            'Iptc4xmpExt:RegionBoundary': region,
            'Iptc4xmpExt:rRole': [{
                'Iptc4xmpExt:Name': {'en-GB': 'main subject area'},
                'xmp:Identifier': [
                    'http://cv.iptc.org/newscodes/imageregionrole/mainSubjectArea'],
                }],
            }

    def to_xmp(self):
        return self

    def is_main_subject_area(self):
        if 'Iptc4xmpExt:rRole' not in self:
            return False
        for role in self['Iptc4xmpExt:rRole']:
            if 'xmp:Identifier' not in role:
                continue
            if ('http://cv.iptc.org/newscodes/imageregionrole/mainSubjectArea'
                    in role['xmp:Identifier']):
                return True
        return False

    def short_keys(self, value):
        if isinstance(value, dict):
            return dict((k.split(':')[-1], self.short_keys(v))
                        for (k, v) in value.items())
        if isinstance(value, list):
            return [self.short_keys(v) for v in value]
        return value

    def __str__(self):
        return pprint.pformat(self.short_keys(self), compact=True)


class MD_ImageRegion(MD_Tuple):
    _type = ImageRegionItem

    def index(self, other):
        if other.is_main_subject_area():
            # only one main subject area region allowed
            for n, value in enumerate(self):
                if value.is_main_subject_area():
                    return True
            return len(self)
        for n, value in enumerate(self):
            if value == other:
                return n
            key = 'Iptc4xmpExt:rId'
            if key in value and key in other and value[key] == other[key]:
                return n
        return len(self)

    @staticmethod
    def rectangle_from_note(note, dims):
        if not ('x' in note and 'y' in note and
                'w' in note and 'h' in note):
            return None
        w, h = dims
        boundary = {'Iptc4xmpExt:rbShape': 'rectangle',
                    'Iptc4xmpExt:rbUnit': 'relative',
                    'Iptc4xmpExt:rbX': round(float(note['x']) / w, 4),
                    'Iptc4xmpExt:rbY': round(float(note['y']) / h, 4),
                    'Iptc4xmpExt:rbW': round(float(note['w']) / w, 4),
                    'Iptc4xmpExt:rbH': round(float(note['h']) / h, 4)}
        return {'Iptc4xmpExt:rRole': [{
                    'Iptc4xmpExt:Name': {'en-GB': 'subject area'},
                    'xmp:Identifier': [
                        'http://cv.iptc.org/newscodes/imageregionrole/subjectArea']}],
                'Iptc4xmpExt:RegionBoundary': boundary}

    @classmethod
    def from_flickr(cls, notes, people, dims):
        w, h = dims
        result = []
        for note in notes:
            region = cls.rectangle_from_note(note, dims)
            region['Iptc4xmpExt:rId'] = 'flickr:' + note['id']
            if '_content' in note:
                region['dc:description'] = {'x-default': note['_content']}
                region['photoshop:CaptionWriter'] = note['authorrealname']
            result.append(region)
        for person in people:
            region = cls.rectangle_from_note(person, dims)
            if not region:
                continue
            region['Iptc4xmpExt:rId'] = 'flickr:' + person['nsid']
            region['Iptc4xmpExt:PersonInImage'] = person['realname']
            region['Iptc4xmpExt:rCtype'] = [{
                    'Iptc4xmpExt:Name': {'en-GB': 'human'},
                    'xmp:Identifier': [
                        'http://cv.iptc.org/newscodes/imageregiontype/human'],
                    }]
            result.append(region)
        return result

    @classmethod
    def from_ipernity(cls, notes, dims):
        w, h = dims
        result = []
        for note in notes:
            region = cls.rectangle_from_note(note, dims)
            region['Iptc4xmpExt:rId'] = 'ipernity:' + note['note_id']
            if 'membername' in note:
                region['Iptc4xmpExt:PersonInImage'] = note['membername']
                region['Iptc4xmpExt:rCtype'] = [{
                    'Iptc4xmpExt:Name': {'en-GB': 'human'},
                    'xmp:Identifier': [
                        'http://cv.iptc.org/newscodes/imageregiontype/human']}]
            if 'content' in note:
                region['dc:description'] = {'x-default': note['content']}
                region['photoshop:CaptionWriter'] = note['username']
            result.append(region)
        return result
